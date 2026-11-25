"""Tabular reports over a crawl (like Screaming Frog's tabs) + CSV / XLSX / JSON export."""
from __future__ import annotations

import csv
import io
import json
from collections import Counter
from http import HTTPStatus
from urllib.parse import urlparse

from . import analysis as an
from .audit import Issue
from .crawler import SiteData
from .structured import validate_page

REPORTS: dict[str, tuple[str, callable]] = {}


def report(name: str, title: str):
    def deco(fn):
        REPORTS[name] = (title, fn)
        return fn
    return deco


def _status_text(code: int) -> str:
    try:
        return HTTPStatus(code).phrase
    except ValueError:
        return "No response" if code == 0 else ""


def _kb(n) -> float:
    return round((n or 0) / 1024, 1)


# ---------------------------------------------------------------- core
@report("internal", "Internal pages")
def r_internal(site: SiteData, **_):
    inl, sc = an.inlinks(site), an.link_score(site)
    cols = ["URL", "Status", "Indexability", "Reason", "Depth", "Title", "Title len", "Meta desc len", "H1", "Words", "Load ms", "Size KB",
            "Inlinks", "Outlinks", "Link score", "Canonical", "Lang", "In sitemap", "Schema", "Readability", "Text ratio %"]
    rows = []
    for p in site.pages.values():
        ix = an.indexability(site, p)
        rows.append([p.url, p.status, ix[0], ix[1], p.depth, p.title, len(p.title), len(p.meta_description), p.h1[0] if p.h1 else "",
                     p.word_count, p.load_ms, _kb(p.size_bytes), len(inl.get(p.url, [])), len(p.internal_links), sc.get(p.url, ""),
                     an.canonical_status(site, p), p.lang, "Yes" if p.in_sitemap else "No", ", ".join(sorted(set(p.schema_types))),
                     p.flesch or "", p.text_ratio])
    return cols, rows


@report("response_codes", "Response codes")
def r_codes(site: SiteData, **_):
    inl = an.inlinks(site)
    return (["URL", "Status", "Status text", "Redirects to", "Inlinks", "Error / note"],
            [[p.url, p.status, "Blocked by robots.txt" if p.blocked_by_robots else _status_text(p.status), p.redirect_final,
              len(inl.get(p.url, [])), p.error or p.redirect_issue] for p in site.pages.values()])


@report("redirects", "Redirects & chains")
def r_redirects(site: SiteData, **_):
    rows = []
    for p in site.pages.values():
        if p.redirect_hops:
            chain = " → ".join([f"{u} [{s}]" for u, s in p.redirect_hops] + [p.redirect_final])
            rows.append([p.url, len(p.redirect_hops), p.status, p.redirect_final, p.redirect_final_status, p.redirect_issue, chain])
    return ["URL", "Hops", "First status", "Final URL", "Final status", "Problem", "Chain"], sorted(rows, key=lambda r: -r[1])


@report("broken_links", "Broken links (with sources)")
def r_broken(site: SiteData, **_):
    anchors = {(p.url, l[0]): l[1] for p in site.pages.values() for l in p.links}
    rows = []
    for s, t, c in site.broken_links:
        kind = "resource" if t in site.resources else "external" if t in site.external_status else "internal"
        rows.append([s, t, c, _status_text(c), anchors.get((s, t), ""), kind])
    return ["Source", "Broken URL", "Status", "Status text", "Anchor text", "Type"], rows


@report("inlinks", "Internal links (all)")
def r_inlinks(site: SiteData, **_):
    rows = []
    for tgt, lst in an.inlinks(site).items():
        for src, anchor, nf in lst:
            rows.append([tgt, src, anchor, "Yes" if nf else "No"])
            if len(rows) >= 100_000:
                break
    return ["Target", "Source", "Anchor text", "Nofollow"], rows


@report("external", "External links")
def r_external(site: SiteData, **_):
    refs: dict[str, list[str]] = {}
    for p in site.pages.values():
        for u in p.external_links:
            refs.setdefault(u, []).append(p.url)
    return (["External URL", "Status", "Linked from (pages)", "First source"],
            [[u, site.external_status.get(u, "not checked"), len(v), v[0]] for u, v in refs.items()])


@report("images", "Images")
def r_images(site: SiteData, **_):
    rows = []
    for p in site.pages.values():
        for im in p.image_list:
            res = site.resources.get(im["src"], {})
            rows.append([p.url, im["src"], im["alt"] if im["alt"] is not None else "(missing)", len(im["alt"] or ""), im["w"] or "", im["h"] or "",
                         _kb(res.get("size")), res.get("status", ""), "Yes" if im["lazy"] else "No"])
    return ["Page", "Image URL", "Alt text", "Alt len", "Width", "Height", "Size KB", "Status", "Lazy"], rows


@report("resources", "CSS / JS / image files")
def r_resources(site: SiteData, **_):
    return (["URL", "Type", "Status", "Size KB", "Used on pages", "Content type"],
            [[u, r["kind"], r["status"], _kb(r["size"]), r["ref_count"], r["ctype"]] for u, r in site.resources.items()])


@report("titles", "Page titles")
def r_titles(site: SiteData, **_):
    dup = Counter(p.title.strip().lower() for p in an.html_pages(site) if p.title)
    return (["URL", "Title", "Length", "Pixel width", "Title tags", "Duplicates", "Same as H1"],
            [[p.url, p.title, len(p.title), an.pixel_width(p.title), p.title_count, dup[p.title.strip().lower()] if p.title else 0,
              "Yes" if p.h1 and p.title.strip().lower() == p.h1[0].strip().lower() else "No"] for p in an.html_pages(site)])


@report("descriptions", "Meta descriptions")
def r_desc(site: SiteData, **_):
    dup = Counter(p.meta_description.strip().lower() for p in an.html_pages(site) if p.meta_description)
    return (["URL", "Meta description", "Length", "Pixel width", "Tags", "Duplicates"],
            [[p.url, p.meta_description, len(p.meta_description), an.pixel_width(p.meta_description, 14), p.meta_desc_count,
              dup[p.meta_description.strip().lower()] if p.meta_description else 0] for p in an.html_pages(site)])


@report("headings", "Headings")
def r_headings(site: SiteData, **_):
    return (["URL", "H1", "H1 count", "H1 length", "H2 count", "First H2", "Outline"],
            [[p.url, p.h1[0] if p.h1 else "", len(p.h1), len(p.h1[0]) if p.h1 else 0, len(p.h2), p.h2[0] if p.h2 else "",
              " > ".join(f"H{l}" for l, _ in p.headings[:12])] for p in an.html_pages(site)])


@report("directives", "Directives & canonicals")
def r_directives(site: SiteData, **_):
    rows = []
    for p in site.pages.values():
        if not p.is_html and not p.blocked_by_robots:
            continue
        ix = an.indexability(site, p)
        rows.append([p.url, p.robots_meta, p.x_robots, ix[0], ix[1], p.canonical or p.canonical_header, an.canonical_status(site, p),
                     an.canonical_target(p), p.canonical_count, p.meta_refresh, p.rel_next, p.rel_prev, p.amp_url])
    return (["URL", "Meta robots", "X-Robots-Tag", "Indexability", "Reason", "Canonical", "Canonical status", "Canonical target",
             "Canonical tags", "Meta refresh", "rel=next", "rel=prev", "AMP"], rows)


@report("hreflang", "Hreflang")
def r_hreflang(site: SiteData, **_):
    by = {an.norm_url(p.url): p for p in site.pages.values()}
    rows = []
    for p in an.html_pages(site):
        for lang, href in p.hreflang:
            t = by.get(an.norm_url(href))
            back = "n/a" if not t else "Yes" if any(an.norm_url(h) == an.norm_url(p.url) for _, h in t.hreflang) or t is p else "NO"
            rows.append([p.url, lang, href, t.status if t else "not crawled", back, "Yes" if an.HREFLANG_RE.match(lang) else "INVALID"])
    return ["Page", "Hreflang", "Alternate URL", "Target status", "Return link", "Valid code"], rows


@report("duplicates_exact", "Exact duplicates")
def r_dup_exact(site: SiteData, **_):
    rows = []
    for i, g in enumerate(an.exact_duplicates(site), 1):
        for u in g["urls"]:
            rows.append([i, g["type"], u, site.pages[u].title, site.pages[u].word_count])
    return ["Group", "Match type", "URL", "Title", "Words"], rows


@report("duplicates_near", "Near duplicates")
def r_dup_near(site: SiteData, threshold: float = 0.9, **_):
    return ["URL A", "URL B", "Similarity %"], [[n["a"], n["b"], n["similarity"]] for n in an.near_duplicates(site, float(threshold))]


@report("semantic", "Semantic similarity")
def r_semantic(site: SiteData, threshold: float = 0.6, **_):
    return ["URL A", "URL B", "Similarity %"], [[n["a"], n["b"], n["similarity"]] for n in an.semantic_pairs(site, float(threshold))]


@report("clusters", "Content clusters")
def r_clusters(site: SiteData, threshold: float = 0.22, **_):
    rows = []
    for i, c in enumerate(an.clusters(site, float(threshold)), 1):
        for u in c["urls"]:
            rows.append([i, c["label"], len(c["urls"]), u, site.pages[u].title])
    return ["Cluster", "Topic terms", "Cluster size", "URL", "Title"], rows


@report("low_relevance", "Low-relevance content")
def r_lowrel(site: SiteData, **_):
    return ["URL", "Relevance to site topic %"], [[r["url"], r["relevance"]] for r in an.low_relevance(site)]


@report("structured", "Structured data")
def r_structured(site: SiteData, **_):
    rows = []
    for p in an.html_pages(site):
        for r in validate_page(p):
            rows.append([p.url, r["format"], r["type"], "Yes" if r["rich"] else "No", len(r["errors"]), "; ".join(r["errors"]),
                         len(r["warnings"]), "; ".join(r["warnings"])])
    return ["URL", "Format", "Type", "Rich-result type", "Errors", "Error detail", "Warnings", "Warning detail"], rows


@report("mobile", "Mobile usability")
def r_mobile(site: SiteData, **_):
    rows = []
    for p in an.html_pages(site):
        m, r = p.mobile_static, p.mobile_render
        rows.append([p.url, "Yes" if m.get("viewport_ok") else "No", "Yes" if p.a11y.get("zoom_disabled") else "No", m.get("fixed_width_elements", 0),
                     m.get("small_font_inline", 0), m.get("plugins", 0), r.get("overflow_x", "n/a (enable JS rendering + mobile)"),
                     r.get("small_tap_targets", "n/a"), r.get("small_text", "n/a")])
    return ["URL", "Responsive viewport", "Zoom disabled", "Fixed-width elements", "Small inline fonts", "Plugins", "Horizontal overflow",
            "Small tap targets", "Small text nodes"], rows


@report("accessibility", "Accessibility")
def r_a11y(site: SiteData, **_):
    keys = ["input_no_label", "button_no_name", "link_no_name", "img_missing_alt_attr", "empty_heading", "iframe_no_title", "duplicate_ids",
            "tabindex_positive", "table_no_headers", "autoplay_media", "zoom_disabled", "no_main_landmark", "no_skip_link", "no_lang"]
    rows = [[p.url, sum(p.a11y.get(k, 0) for k in keys)] + [p.a11y.get(k, 0) for k in keys] for p in an.html_pages(site)]
    return ["URL", "Total"] + keys, sorted(rows, key=lambda r: -r[1])


@report("axe", "Accessibility (axe-core, needs JS rendering)")
def r_axe(site: SiteData, **_):
    return (["URL", "Rule", "Impact", "Description", "Elements"],
            [[p.url, v["id"], v["impact"], v["help"], v["nodes"]] for p in site.pages.values() for v in p.axe])


@report("security", "Security headers")
def r_security(site: SiteData, **_):
    y = lambda p, h: "Yes" if h in p.headers else "No"
    return (["URL", "HSTS", "X-Content-Type-Options", "X-Frame-Options", "CSP", "Referrer-Policy", "Mixed content", "Insecure forms"],
            [[p.url, y(p, "strict-transport-security"), y(p, "x-content-type-options"), y(p, "x-frame-options"),
              y(p, "content-security-policy"), y(p, "referrer-policy"), len(p.mixed_content), p.insecure_forms] for p in an.html_pages(site)])


@report("spelling", "Spelling & grammar")
def r_spelling(site: SiteData, **_):
    rows = []
    for r in an.spelling_grammar(site):
        rows += [[r["url"], "Spelling", s["word"], s["suggestion"]] for s in r["spelling"]]
        rows += [[r["url"], "Grammar", g["issue"], g["context"]] for g in r["grammar"]]
    return ["URL", "Type", "Issue / word", "Suggestion / context"], rows


@report("url_issues", "URL quality")
def r_urls(site: SiteData, **_):
    return ["URL", "Problem"], [[p.url, prob] for p in site.pages.values() for prob in an.url_problems(p.url)]


@report("sitemaps", "Sitemap files")
def r_sitemaps(site: SiteData, **_):
    return ["Sitemap", "Type", "URLs", "Size KB", "Status"], [[s["url"], s["type"], s["urls"], _kb(s["bytes"]), s["status"]] for s in site.sitemaps]


@report("sitemap_urls", "Sitemap URLs vs crawl")
def r_smurls(site: SiteData, **_):
    by = {an.norm_url(p.url): p for p in site.pages.values()}
    rows = []
    for u in site.sitemap_urls:
        p = by.get(an.norm_url(u))
        m = site.sitemap_meta.get(u, {})
        rows.append([u, p.status if p else "not crawled", " ".join(an.indexability(site, p)) if p else "", m.get("lastmod", ""), m.get("sitemap", "")])
    return ["URL", "Crawl status", "Indexability", "lastmod", "Sitemap file"], rows


@report("custom_search", "Custom search")
def r_csearch(site: SiteData, **_):
    names = sorted({k for p in site.pages.values() for k in p.custom_search})
    return ["URL"] + names, [[p.url] + [p.custom_search.get(n, "") for n in names] for p in site.pages.values() if p.custom_search]


@report("custom_extraction", "Custom extraction")
def r_cextract(site: SiteData, **_):
    names = sorted({k for p in site.pages.values() for k in p.extractions})
    return (["URL"] + names, [[p.url] + [" | ".join(p.extractions.get(n, [])) for n in names]
                              for p in site.pages.values() if p.extractions])


@report("javascript", "JavaScript rendering")
def r_js(site: SiteData, **_):
    return (["URL", "Words (raw HTML)", "Words (rendered)", "Links (raw)", "Links (rendered)", "Failed resources", "Console errors", "Custom JS result"],
            [[p.url, p.raw_word_count, p.word_count, p.raw_link_count, len(p.internal_links), len(p.js_failed_resources), p.js_console_errors,
              json.dumps(p.js_result)[:300] if p.js_result is not None else ""] for p in an.html_pages(site) if p.raw_word_count or p.js_result is not None])


@report("orphans", "Orphan pages")
def r_orphans(site: SiteData, **_):
    inl = an.inlinks(site)
    return (["URL", "In sitemap", "Indexability"],
            [[p.url, "Yes" if p.in_sitemap else "No", an.indexability(site, p)[0]] for p in an.html_pages(site)
             if not inl.get(p.url) and p.url != next(iter(site.pages))])


@report("keywords", "Keywords (site vocabulary)")
def r_keywords(site: SiteData, **_):
    from .keywords import extract_keywords
    return ["Keyword", "Occurrences", "Pages"], [[k["keyword"], k["occurrences"], k["pages"]] for k in extract_keywords(site, 200)]


# ---------------------------------------------------------------- issue exports
def issues_table(issues: list[Issue]):
    rows = []
    for i in issues:
        for u in (i.urls or [""]):
            rows.append([i.severity, i.category, i.id, i.message, u, i.fix_hint])
    return ["Severity", "Category", "Issue ID", "Issue", "URL / detail", "How to fix"], rows


def get(site: SiteData, name: str, **params):
    if name not in REPORTS:
        raise KeyError(name)
    return REPORTS[name][1](site, **params)


def catalog() -> list[dict]:
    return [{"name": k, "title": v[0]} for k, v in REPORTS.items()]


# ---------------------------------------------------------------- export formats
def to_csv(cols: list[str], rows: list[list]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    w.writerows(rows)
    return buf.getvalue()


def to_xlsx(sheets: dict[str, tuple[list[str], list[list]]]) -> bytes:
    from openpyxl import Workbook
    wb = Workbook()
    wb.remove(wb.active)
    for name, (cols, rows) in sheets.items():
        ws = wb.create_sheet(name[:31])
        ws.append(cols)
        for r in rows[:1_000_000]:
            ws.append([("" if v is None else v if isinstance(v, (int, float, str)) else str(v)) for v in r])
        ws.freeze_panes = "A2"
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def full_workbook(site: SiteData, issues: list[Issue], names: list[str] | None = None) -> bytes:
    sheets = {"Issues": issues_table(issues)}
    for n in names or ["internal", "response_codes", "redirects", "broken_links", "titles", "descriptions", "directives", "hreflang",
                       "duplicates_exact", "duplicates_near", "structured", "images", "security", "accessibility", "url_issues", "sitemap_urls"]:
        sheets[REPORTS[n][0]] = get(site, n)
    return to_xlsx(sheets)
