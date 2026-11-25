"""Derived analyses over a crawl: indexability, link graph/score, duplicates, similarity, clusters, spelling,
hreflang, sitemap generation, segmentation. All pure functions of SiteData (so they work on saved crawls too)."""
from __future__ import annotations

import heapq
import math
import re
import zlib
from collections import Counter, defaultdict
from urllib.parse import unquote, urlparse
from xml.sax.saxutils import escape

from .crawler import Page, SiteData, canon

STOP = set("""a about above after again all also am an and any are as at be because been before being below between both but by
can could did do does doing down during each few for from further had has have having he her here hers him his how i if in into
is it its just me more most my no nor not now of off on once only or other our out over own same she should so some such than that
the their them then there these they this those through to too under until up us very was we were what when where which while who
whom why will with would you your yours get got one two new use used using may might must also like make made many much per via""".split())


# ------------------------------------------------------------------ small helpers
def html_pages(site: SiteData) -> list[Page]:
    return [p for p in site.pages.values() if p.is_html and p.status == 200 and not p.error]


def _cached(site: SiteData, key: str, fn):
    if key not in site.derived:
        site.derived[key] = fn()
    return site.derived[key]


_NARROW, _WIDE = set("ijlI.,:;'!|()[]"), set("mwMW@%")
_UP = set("ABCDEFGHJKLNOPQRSTUVXYZ0123456789")


def pixel_width(text: str, px: int = 20, bold: bool = False) -> int:
    """Approximate rendered width in Arial (Google SERP uses ~20px titles, ~14px descriptions)."""
    w = 0.0
    for ch in text:
        w += 0.25 if ch in _NARROW else 0.28 if ch == " " else 0.85 if ch in _WIDE else 0.68 if ch in _UP else 0.52
    return round(w * px * (1.05 if bold else 1))


def norm_url(u: str) -> str:
    return (u or "").strip().rstrip("/").lower()


def url_problems(url: str) -> list[str]:
    p = urlparse(url)
    out = []
    path = unquote(p.path)
    if len(url) > 115:
        out.append("URL longer than 115 characters")
    if any(c.isupper() for c in p.path):
        out.append("Uppercase characters in path")
    if "_" in p.path:
        out.append("Underscores in path (use hyphens)")
    if " " in path or "%20" in p.path:
        out.append("Spaces in URL")
    if any(ord(c) > 127 for c in path):
        out.append("Non-ASCII characters")
    if p.query.count("&") >= 2 or len(p.query) > 60:
        out.append("Complex query string (many parameters)")
    if "//" in p.path:
        out.append("Double slash in path")
    segs = [s for s in p.path.split("/") if s]
    if len(segs) != len(set(segs)) and len(segs) > 2:
        out.append("Repeated path segments")
    if p.scheme == "http":
        out.append("Not HTTPS")
    return out


# ------------------------------------------------------------------ indexability
def indexability(site: SiteData, p: Page) -> tuple[str, str]:
    if p.blocked_by_robots:
        return "Non-Indexable", "Blocked by robots.txt"
    if p.error:
        return "Non-Indexable", "No response"
    if p.redirect_hops:
        return "Non-Indexable", "Redirected"
    if 400 <= p.status < 500:
        return "Non-Indexable", "Client error"
    if p.status >= 500:
        return "Non-Indexable", "Server error"
    if not p.is_html:
        return "Non-HTML", ""
    directives = (p.robots_meta + "," + p.x_robots).lower()
    if "noindex" in directives or re.search(r"\bnone\b", directives):
        return "Non-Indexable", "noindex"
    c = canonical_target(p)
    if c and norm_url(c) != norm_url(p.url) and norm_url(c) != norm_url(p.final_url):
        return "Non-Indexable", "Canonicalised"
    return "Indexable", ""


def canonical_target(p: Page) -> str:
    from urllib.parse import urljoin
    c = p.canonical or p.canonical_header
    return canon(urljoin(p.final_url or p.url, c)) if c else ""


def canonical_status(site: SiteData, p: Page) -> str:
    c = canonical_target(p)
    if not p.is_html or p.status != 200:
        return ""
    if not c:
        return "Missing"
    if norm_url(c) == norm_url(p.url):
        return "Self-referencing"
    return "Canonicalised"


# ------------------------------------------------------------------ links
def inlinks(site: SiteData) -> dict[str, list[tuple[str, str, int]]]:
    def build():
        d: dict[str, list] = defaultdict(list)
        for p in site.pages.values():
            for url, anchor, nf, internal in p.links:
                if internal and url != p.url:
                    d[url].append((p.url, anchor, nf))
            if p.redirect_final:
                d[p.redirect_final].append((p.url, "(redirect)", 0))
        return d
    return _cached(site, "inlinks", build)


def link_score(site: SiteData) -> dict[str, int]:
    """Internal PageRank, scaled 1-100 (like Screaming Frog's Link Score)."""
    def build():
        urls = [u for u, p in site.pages.items() if p.is_html or p.redirect_hops]
        if not urls:
            return {}
        idx = {u: i for i, u in enumerate(urls)}
        out: list[list[int]] = [[] for _ in urls]
        for u in urls:
            p = site.pages[u]
            targets = {idx[l[0]] for l in p.links if l[3] and l[0] in idx and l[0] != u and not l[2]}
            if p.redirect_final in idx:
                targets.add(idx[p.redirect_final])
            out[idx[u]] = list(targets)
        n, d = len(urls), 0.85
        pr = [1 / n] * n
        for _ in range(30):
            new = [(1 - d) / n] * n
            dangling = sum(pr[i] for i in range(n) if not out[i])
            for i in range(n):
                if out[i]:
                    share = d * pr[i] / len(out[i])
                    for j in out[i]:
                        new[j] += share
            add = d * dangling / n
            pr = [x + add for x in new]
        lo, hi = min(pr), max(pr)
        span = (hi - lo) or 1
        return {u: max(1, round(1 + 99 * (pr[idx[u]] - lo) / span)) for u in urls}
    return _cached(site, "link_score", build)


def graph_data(site: SiteData, limit: int = 250) -> dict:
    """Nodes/edges for the force-directed visualisation (top pages by link score)."""
    scores = link_score(site)
    pages = sorted((p for p in site.pages.values() if p.is_html and p.status == 200), key=lambda p: -scores.get(p.url, 0))[:limit]
    keep = {p.url for p in pages}
    nodes = [{"id": p.url, "label": urlparse(p.url).path or "/", "depth": p.depth, "score": scores.get(p.url, 1),
              "indexable": indexability(site, p)[0] == "Indexable", "folder": (urlparse(p.url).path.split("/") + ["", ""])[1]}
             for p in pages]
    edges = []
    for p in pages:
        for l in p.links:
            if l[3] and l[0] in keep and l[0] != p.url:
                edges.append([p.url, l[0]])
    return {"nodes": nodes, "edges": edges[:3000]}


def directory_tree(site: SiteData) -> dict:
    root: dict = {"name": "/", "count": 0, "children": {}}
    for p in html_pages(site):
        node = root
        node["count"] += 1
        for seg in [s for s in urlparse(p.url).path.split("/") if s]:
            node = node["children"].setdefault(seg, {"name": seg, "count": 0, "children": {}})
            node["count"] += 1

    def conv(n):
        return {"name": n["name"], "count": n["count"],
                "children": sorted((conv(c) for c in n["children"].values()), key=lambda x: -x["count"])}
    return conv(root)


def depth_histogram(site: SiteData) -> dict[int, int]:
    return dict(sorted(Counter(p.depth for p in html_pages(site)).items()))


# ------------------------------------------------------------------ duplicates
def exact_duplicates(site: SiteData) -> list[dict]:
    """Groups of indexable pages with identical body text (content hash) or identical HTML."""
    groups: dict[tuple, list[str]] = defaultdict(list)
    for p in html_pages(site):
        if p.content_hash and p.word_count >= 10:
            groups[("content", p.content_hash)].append(p.url)
    out = [{"type": "content", "urls": u} for (_, _), u in groups.items() if len(u) > 1]
    html_groups: dict[str, list[str]] = defaultdict(list)
    for p in html_pages(site):
        if p.html_hash:
            html_groups[p.html_hash].append(p.url)
    seen = {tuple(sorted(g["urls"])) for g in out}
    for u in html_groups.values():
        if len(u) > 1 and tuple(sorted(u)) not in seen:
            out.append({"type": "html", "urls": u})
    return sorted(out, key=lambda g: -len(g["urls"]))


def _shingles(text: str, k: int = 4) -> set[int]:
    w = re.findall(r"\w+", text.lower())
    return {zlib.crc32(" ".join(w[i:i + k]).encode()) for i in range(max(0, len(w) - k + 1))}


def near_duplicates(site: SiteData, threshold: float = 0.9, sketch: int = 128) -> list[dict]:
    """Near-duplicate pages via bottom-k MinHash sketches + inverted-index candidate search."""
    def build():
        pages = [p for p in html_pages(site) if p.word_count >= 30]
        sk: dict[str, list[int]] = {}
        sets: dict[str, int] = {}
        for p in pages:
            sh = _shingles(p.text)
            if sh:
                sk[p.url] = heapq.nsmallest(sketch, sh)
                sets[p.url] = len(sh)
        post: dict[int, list[str]] = defaultdict(list)
        for u, s in sk.items():
            for h in s:
                post[h].append(u)
        pair_counts: Counter = Counter()
        for us in post.values():
            if 1 < len(us) <= 200:
                for i in range(len(us)):
                    for j in range(i + 1, len(us)):
                        pair_counts[(us[i], us[j])] += 1
        res = []
        for (a, b), c in pair_counts.items():
            if c < threshold * 0.5 * min(len(sk[a]), len(sk[b])):
                continue
            sa, sb = set(sk[a]), set(sk[b])
            union = heapq.nsmallest(sketch, sa | sb)
            est = sum(1 for h in union if h in sa and h in sb) / max(len(union), 1)
            if est >= threshold:
                res.append({"a": a, "b": b, "similarity": round(est * 100, 1)})
        return sorted(res, key=lambda r: -r["similarity"])
    key = f"near_{threshold}"
    return _cached(site, key, build)


# ------------------------------------------------------------------ semantic similarity / clusters
def _tokens(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-zA-Z][a-zA-Z'-]{2,}", text.lower()) if w not in STOP]


def tfidf_vectors(site: SiteData, top_terms: int = 80) -> dict[str, dict[str, float]]:
    def build():
        pages = [p for p in html_pages(site) if p.word_count >= 30]
        docs = {p.url: Counter(_tokens(p.title + " " + " ".join(p.h1) + " " + p.text)) for p in pages}
        n = len(docs) or 1
        df: Counter = Counter()
        for c in docs.values():
            df.update(c.keys())
        vecs = {}
        for u, c in docs.items():
            w = {t: (1 + math.log(f)) * math.log((n + 1) / (df[t] + 0.5)) for t, f in c.items() if df[t] <= 0.6 * n or n < 5}
            w = dict(heapq.nlargest(top_terms, w.items(), key=lambda kv: kv[1]))
            norm = math.sqrt(sum(x * x for x in w.values())) or 1
            vecs[u] = {t: x / norm for t, x in w.items()}
        return vecs
    return _cached(site, "tfidf", build)


def semantic_pairs(site: SiteData, threshold: float = 0.8, vectors: dict | None = None) -> list[dict]:
    vecs = vectors if vectors is not None else tfidf_vectors(site)
    if vectors is not None:  # dense embeddings (numpy)
        import numpy as np
        urls = list(vecs)
        m = np.array([vecs[u] for u in urls], dtype="float32")
        m /= np.linalg.norm(m, axis=1, keepdims=True) + 1e-9
        sim = m @ m.T
        res = []
        for i in range(len(urls)):
            for j in range(i + 1, len(urls)):
                if sim[i, j] >= threshold:
                    res.append({"a": urls[i], "b": urls[j], "similarity": round(float(sim[i, j]) * 100, 1)})
        return sorted(res, key=lambda r: -r["similarity"])[:2000]
    post: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for u, v in vecs.items():
        for t, w in v.items():
            post[t].append((u, w))
    acc: dict[tuple[str, str], float] = defaultdict(float)
    for t, lst in post.items():
        if len(lst) > 300:
            continue
        for i in range(len(lst)):
            for j in range(i + 1, len(lst)):
                a, b = lst[i][0], lst[j][0]
                acc[(a, b) if a < b else (b, a)] += lst[i][1] * lst[j][1]
    res = [{"a": a, "b": b, "similarity": round(s * 100, 1)} for (a, b), s in acc.items() if s >= threshold]
    return sorted(res, key=lambda r: -r["similarity"])[:2000]


def clusters(site: SiteData, threshold: float = 0.22, vectors: dict | None = None) -> list[dict]:
    """Leader clustering on TF-IDF (or embedding) vectors; each cluster is labelled by its top terms."""
    if vectors is not None:
        import numpy as np
        urls = list(vecs := vectors)
        m = np.array([vecs[u] for u in urls], dtype="float32")
        m /= np.linalg.norm(m, axis=1, keepdims=True) + 1e-9
        cents: list = []
        members: list[list[int]] = []
        for i, v in enumerate(m):
            best, bs = -1, 0.0
            for ci, c in enumerate(cents):
                s = float(v @ (c / (np.linalg.norm(c) + 1e-9)))
                if s > bs:
                    best, bs = ci, s
            if best >= 0 and bs >= threshold:
                cents[best] = cents[best] + v
                members[best].append(i)
            else:
                cents.append(v.copy())
                members.append([i])
        out = [{"label": "", "urls": [urls[i] for i in ms]} for ms in members]
    else:
        vecs = tfidf_vectors(site)
        cents_s: list[dict[str, float]] = []
        mem: list[list[str]] = []
        for u, v in vecs.items():
            best, bs = -1, 0.0
            for ci, c in enumerate(cents_s):
                norm = math.sqrt(sum(x * x for x in c.values())) or 1
                s = sum(w * c.get(t, 0) for t, w in v.items()) / norm
                if s > bs:
                    best, bs = ci, s
            if best >= 0 and bs >= threshold:
                for t, w in v.items():
                    cents_s[best][t] = cents_s[best].get(t, 0) + w
                mem[best].append(u)
            else:
                cents_s.append(dict(v))
                mem.append([u])
        out = [{"label": ", ".join(t for t, _ in heapq.nlargest(3, c.items(), key=lambda kv: kv[1])), "urls": ms}
               for c, ms in zip(cents_s, mem)]
    if vectors is not None:
        for c in out:
            terms: Counter = Counter()
            for u in c["urls"]:
                terms.update(set(_tokens(site.pages[u].title + " " + " ".join(site.pages[u].h1))))
            c["label"] = ", ".join(t for t, _ in terms.most_common(3))
    return sorted(out, key=lambda c: -len(c["urls"]))


def low_relevance(site: SiteData, cutoff: float = 0.08) -> list[dict]:
    """Pages whose content is far from the site's overall topic centroid."""
    vecs = tfidf_vectors(site)
    if len(vecs) < 5:
        return []
    cent: dict[str, float] = defaultdict(float)
    for v in vecs.values():
        for t, w in v.items():
            cent[t] += w
    norm = math.sqrt(sum(x * x for x in cent.values())) or 1
    res = [{"url": u, "relevance": round(sum(w * cent.get(t, 0) for t, w in v.items()) / norm * 100, 1)} for u, v in vecs.items()]
    return sorted([r for r in res if r["relevance"] / 100 < cutoff], key=lambda r: r["relevance"])


# ------------------------------------------------------------------ spelling & grammar
GRAMMAR = [
    (re.compile(r"\b(\w+)\s+\1\b", re.I), "Repeated word", lambda m: m.group(1).lower() not in ("had", "that", "very", "bye", "no")),
    (re.compile(r"\b(could|should|would|must) of\b", re.I), "'of' should be 'have'", None),
    (re.compile(r"\b(a lot) (of)? ?(?=\w)", re.I), None, None),
    (re.compile(r"\balot\b", re.I), "'alot' should be 'a lot'", None),
    (re.compile(r"\bless then\b|\bmore then\b|\bbetter then\b", re.I), "'then' should be 'than'", None),
    (re.compile(r"\byour welcome\b", re.I), "'your' should be 'you're'", None),
    (re.compile(r"\birregardless\b", re.I), "Non-standard word 'irregardless'", None),
    (re.compile(r"\ba ([aeiou]\w+)", re.I), "Use 'an' before a vowel sound",
     lambda m: not re.match(r"(uni|use|usu|eur|one|once|ubiq|uti)", m.group(1), re.I)),
    (re.compile(r"\ban ([bcdfgjklmnpqrstvwxz]\w+)", re.I), "Use 'a' before a consonant sound",
     lambda m: not re.match(r"(hour|honest|honor|heir|mba|seo|html|faq|sms|llm|api|fbi|x-)", m.group(1), re.I)),
    (re.compile(r"\bi (am|was|have|will|think|can|do|would|had)\b"), "'I' should be capitalised", None),
    (re.compile(r"[a-z]{3,}[,;:][a-z]{3,}"), "Missing space after punctuation", None),
]


def spelling_grammar(site: SiteData, max_pages: int = 300, languagetool_url: str = "") -> list[dict]:
    """Per-page spelling mistakes + rule-based grammar issues. Words used on 3+ pages are treated as site vocabulary."""
    def build():
        from spellchecker import SpellChecker
        pages = html_pages(site)[:max_pages]
        langs = {"en", "es", "fr", "de", "pt", "it", "ru", "ar", "nl", "eu", "lv", "fa"}
        checkers: dict[str, SpellChecker] = {}
        page_words: dict[str, Counter] = {}
        df: Counter = Counter()
        for p in pages:
            ws = Counter(w for w in re.findall(r"[^\W\d_]{4,}", p.text[:20000]) if not w[0].isupper() and not w.isupper())
            page_words[p.url] = ws
            df.update(set(ws))
        host_words = set(re.findall(r"[a-z]{3,}", urlparse(site.base).netloc.lower()))
        out = []
        for p in pages:
            lang = (p.lang or "en").lower()[:2]
            lang = lang if lang in langs else "en"
            sc = checkers.setdefault(lang, SpellChecker(language=lang))
            cand = [w for w in page_words[p.url] if df[w] < 3 and w.lower() not in host_words]
            bad = sorted(sc.unknown([w.lower() for w in cand]))[:25]
            spell = [{"word": w, "suggestion": sc.correction(w) or ""} for w in bad]
            gram = []
            if lang == "en":
                for rx, msg, ok in GRAMMAR:
                    if not msg:
                        continue
                    for m in rx.finditer(p.text[:20000]):
                        if ok is None or ok(m):
                            s = max(0, m.start() - 25)
                            gram.append({"issue": msg, "context": "…" + p.text[s:m.end() + 25].strip() + "…"})
                            if len(gram) >= 15:
                                break
                    if len(gram) >= 15:
                        break
            if spell or gram:
                out.append({"url": p.url, "spelling": spell, "grammar": gram})
        return out
    return _cached(site, "spell", build)


async def languagetool_check(text: str, url: str, lang: str = "en-US") -> list[dict]:
    import httpx
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(url.rstrip("/") + "/v2/check", data={"text": text[:20000], "language": lang})
        r.raise_for_status()
        return [{"issue": m["message"], "context": m["context"]["text"], "rule": m["rule"]["id"]} for m in r.json().get("matches", [])[:40]]


# ------------------------------------------------------------------ hreflang
HREFLANG_RE = re.compile(r"^(x-default|[a-z]{2,3}(-[a-z]{4})?(-([a-z]{2}|\d{3}))?)$", re.I)


def hreflang_findings(site: SiteData) -> dict[str, list[str]]:
    """issue_id -> list of 'page → detail' strings."""
    f: dict[str, list[str]] = defaultdict(list)
    pages = {norm_url(p.url): p for p in site.pages.values()}
    for p in html_pages(site):
        if not p.hreflang:
            continue
        langs = [l.lower() for l, _ in p.hreflang]
        targets = {norm_url(h): l for l, h in p.hreflang}
        if norm_url(p.url) not in targets and norm_url(p.final_url) not in targets:
            f["hreflang_no_self"].append(p.url)
        for lang, c in Counter(langs).items():
            if c > 1:
                f["hreflang_dup_lang"].append(f"{p.url} → {lang} ×{c}")
        if "x-default" not in langs:
            f["hreflang_no_xdefault"].append(p.url)
        can = canonical_target(p)
        if can and norm_url(can) != norm_url(p.url):
            f["hreflang_canonical_conflict"].append(f"{p.url} (canonical → {can})")
        for lang, href in p.hreflang:
            if not HREFLANG_RE.match(lang):
                f["hreflang_bad_code"].append(f"{p.url} → '{lang}'")
            t = pages.get(norm_url(href))
            if t is None:
                continue
            if t.status != 200 or t.redirect_hops or t.error:
                f["hreflang_bad_target"].append(f"{p.url} → {href} [{t.status}]")
            elif "noindex" in (t.robots_meta + t.x_robots):
                f["hreflang_noindex_target"].append(f"{p.url} → {href}")
            elif norm_url(href) != norm_url(p.url) and not any(norm_url(h) == norm_url(p.url) for _, h in t.hreflang):
                f["hreflang_no_return"].append(f"{p.url} → {href} (no return link)")
        if p.lang and p.hreflang:
            own = [l.lower() for l, h in p.hreflang if norm_url(h) == norm_url(p.url)]
            if own and p.lang.lower() not in own and p.lang.lower()[:2] not in [o[:2] for o in own]:
                f["hreflang_lang_mismatch"].append(f"{p.url} (html lang={p.lang}, hreflang={own[0]})")
    return f


# ------------------------------------------------------------------ sitemap generation
def generate_sitemaps(site: SiteData, include_images: bool = False, hreflang: bool = True, priority_by_depth: bool = True,
                      changefreq: str = "", include_noindex: bool = False, max_urls: int = 50000) -> dict[str, str]:
    """XML sitemap(s) from the crawl: indexable 200 pages only. Splits at 50k URLs and adds an index."""
    pages = [p for p in html_pages(site) if include_noindex or indexability(site, p)[0] == "Indexable"]
    pages.sort(key=lambda p: (p.depth, p.url))
    ns = 'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'
    if include_images:
        ns += ' xmlns:image="http://www.google.com/schemas/sitemap-image/1.1"'
    if hreflang:
        ns += ' xmlns:xhtml="http://www.w3.org/1999/xhtml"'
    chunks = [pages[i:i + max_urls] for i in range(0, max(len(pages), 1), max_urls)] or [[]]
    files: dict[str, str] = {}
    for n, chunk in enumerate(chunks, 1):
        lines = ['<?xml version="1.0" encoding="UTF-8"?>', f"<urlset {ns}>"]
        for p in chunk:
            lines.append("  <url>")
            lines.append(f"    <loc>{escape(p.url)}</loc>")
            lm = p.headers.get("last-modified")
            if lm:
                try:
                    from email.utils import parsedate_to_datetime
                    lines.append(f"    <lastmod>{parsedate_to_datetime(lm).date().isoformat()}</lastmod>")
                except Exception:
                    pass
            if changefreq:
                lines.append(f"    <changefreq>{changefreq}</changefreq>")
            if priority_by_depth:
                lines.append(f"    <priority>{max(0.1, round(1.0 - 0.2 * p.depth, 1))}</priority>")
            if hreflang:
                for lang, href in p.hreflang:
                    lines.append(f'    <xhtml:link rel="alternate" hreflang="{escape(lang)}" href="{escape(href)}"/>')
            if include_images:
                for im in p.image_list[:20]:
                    lines.append(f"    <image:image><image:loc>{escape(im['src'])}</image:loc></image:image>")
            lines.append("  </url>")
        lines.append("</urlset>")
        files["sitemap.xml" if len(chunks) == 1 else f"sitemap-{n}.xml"] = "\n".join(lines) + "\n"
    if len(chunks) > 1:
        idx = ['<?xml version="1.0" encoding="UTF-8"?>', '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
        idx += [f"  <sitemap><loc>{escape(site.base)}/{name}</loc></sitemap>" for name in files]
        idx.append("</sitemapindex>")
        files["sitemap_index.xml"] = "\n".join(idx) + "\n"
    return files


def sitemap_findings(site: SiteData) -> dict[str, list[str]]:
    f: dict[str, list[str]] = defaultdict(list)
    by_url = {norm_url(p.url): p for p in site.pages.values()}
    for u in site.sitemap_urls[:5000]:
        p = by_url.get(norm_url(u))
        if not p:
            continue
        status, reason = indexability(site, p)
        if p.redirect_hops:
            f["sitemap_redirect"].append(u)
        elif p.status >= 400 or p.error:
            f["sitemap_error"].append(f"{u} [{p.status}]")
        elif reason == "noindex":
            f["sitemap_noindex"].append(u)
        elif reason == "Canonicalised":
            f["sitemap_non_canonical"].append(u)
        elif reason == "Blocked by robots.txt":
            f["sitemap_blocked"].append(u)
    if site.sitemap_found:
        in_sm = {norm_url(u) for u in site.sitemap_urls}
        f["indexable_not_in_sitemap"] = [p.url for p in html_pages(site)
                                         if indexability(site, p)[0] == "Indexable" and norm_url(p.url) not in in_sm]
    for sm in site.sitemaps:
        if sm["type"] == "urlset" and (sm["urls"] > 50000 or sm["bytes"] > 50 * 1024 * 1024):
            f["sitemap_too_large"].append(sm["url"])
    return f


# ------------------------------------------------------------------ segmentation
def _field(site: SiteData, p: Page, name: str, issue_map: dict[str, set[str]] | None = None):
    path = urlparse(p.url).path
    ix = indexability(site, p)
    table = {
        "url": p.url, "path": path, "folder": (path.split("/") + ["", ""])[1], "status": p.status, "indexability": ix[0],
        "indexability_reason": ix[1], "depth": p.depth, "words": p.word_count, "load_ms": p.load_ms, "title": p.title,
        "h1": p.h1[0] if p.h1 else "", "lang": p.lang, "schema": ",".join(sorted(set(p.schema_types))),
        "inlinks": len(inlinks(site).get(p.url, [])), "size": p.size_bytes, "in_sitemap": p.in_sitemap,
        "canonical": canonical_status(site, p), "content_type": "html" if p.is_html else "other", "meta_description": p.meta_description,
        "link_score": link_score(site).get(p.url, 0),
    }
    if name.startswith("issue:"):
        return name[6:] in (issue_map or {}).get(p.url, set())
    return table.get(name, "")


def _match(v, op: str, target) -> bool:
    try:
        if op in ("gt", "lt", "gte", "lte"):
            a, b = float(v), float(target)
            return {"gt": a > b, "lt": a < b, "gte": a >= b, "lte": a <= b}[op]
        s, t = str(v).lower(), str(target).lower()
        return {"eq": s == t, "ne": s != t, "contains": t in s, "not_contains": t not in s, "regex": re.search(str(target), str(v), re.I) is not None,
                "empty": s == "", "not_empty": s != ""}[op]
    except (ValueError, KeyError, re.error):
        return False


def auto_segments(site: SiteData) -> list[dict]:
    """Default segments: one per top-level folder (top 8) + templates by indexability."""
    folders = Counter((urlparse(p.url).path.split("/") + ["", ""])[1] for p in html_pages(site))
    segs = [{"name": f"/{f}/" if f else "Home / root files", "match": "all",
             "rules": [{"field": "folder", "op": "eq", "value": f}]} for f, _ in folders.most_common(8)]
    segs.append({"name": "Non-indexable", "match": "all", "rules": [{"field": "indexability", "op": "eq", "value": "Non-Indexable"}]})
    segs.append({"name": "Thin (<300 words)", "match": "all", "rules": [{"field": "words", "op": "lt", "value": 300},
                                                                      {"field": "status", "op": "eq", "value": 200}]})
    return segs


def segment_report(site: SiteData, segments: list[dict], issue_urls: dict[str, list[str]]) -> list[dict]:
    issue_map: dict[str, set[str]] = defaultdict(set)
    for iid, urls in issue_urls.items():
        for u in urls:
            issue_map[u.split(" ")[0]].add(iid)
    pages = list(site.pages.values())
    out = []
    for seg in segments:
        rules, mode = seg.get("rules", []), seg.get("match", "all")
        members = []
        for p in pages:
            res = [_match(_field(site, p, r["field"], issue_map), r.get("op", "eq"), r.get("value", "")) if not r["field"].startswith("issue:")
                   else bool(_field(site, p, r["field"], issue_map)) for r in rules]
            if res and (all(res) if mode == "all" else any(res)):
                members.append(p)
        html = [p for p in members if p.is_html and p.status == 200]
        ids: Counter = Counter()
        for p in members:
            ids.update(issue_map.get(p.url, ()))
        out.append({"name": seg["name"], "pages": len(members), "indexable": sum(1 for p in members if indexability(site, p)[0] == "Indexable"),
                    "errors": sum(1 for p in members if p.status >= 400 or p.error),
                    "avg_words": round(sum(p.word_count for p in html) / len(html)) if html else 0,
                    "avg_load_ms": round(sum(p.load_ms for p in html) / len(html)) if html else 0,
                    "issues": sum(ids.values()), "top_issues": [f"{k} ({v})" for k, v in ids.most_common(4)],
                    "urls": [p.url for p in members][:500]})
    return out
