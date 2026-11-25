"""Async crawler: configurable, auth-capable, optional JS rendering. Extracts SEO facts from every page."""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import re
import time
from dataclasses import dataclass, field, fields
from urllib import robotparser
from urllib.parse import urldefrag, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

UA = "SEOSuiteBot/1.0 (+site audit)"
SKIP_EXT = (".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".avif", ".pdf", ".zip", ".css", ".js", ".ico", ".mp4",
            ".mp3", ".woff", ".woff2", ".ttf", ".xml", ".json", ".docx", ".xlsx", ".pptx", ".gz")
KEEP_HEADERS = ("server", "content-type", "content-encoding", "cache-control", "last-modified", "x-robots-tag",
                "strict-transport-security", "content-security-policy", "x-frame-options", "x-content-type-options",
                "referrer-policy", "permissions-policy", "link", "etag", "location")
AXE_CDN = "https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.8.4/axe.min.js"


# ---------------------------------------------------------------- config
@dataclass
class CrawlConfig:
    max_pages: int = 50
    max_depth: int = 0                 # 0 = unlimited
    concurrency: int = 8
    delay_ms: int = 0
    user_agent: str = UA
    respect_robots: bool = True
    respect_nofollow: bool = False     # don't follow rel=nofollow internal links
    include: list = field(default_factory=list)   # regexes; if set, only matching URLs are crawled
    exclude: list = field(default_factory=list)   # regexes; matching URLs are skipped
    crawl_subdomains: bool = False
    crawl_sitemap: bool = False        # seed the queue from sitemap URLs (finds orphans)
    check_external: bool = True
    max_external: int = 300
    check_resources: bool = True       # HEAD-check images/CSS/JS
    max_resources: int = 500
    max_redirects: int = 10
    timeout: int = 15
    verify_tls: bool = True
    # password protected sites
    auth: dict = field(default_factory=dict)  # {basic:{user,password}|digest:{..}, headers:{}, cookies:{}, login:{url,method,data,csrf}}
    # rendering
    js_render: bool = False
    mobile: bool = False               # render as a phone and collect mobile usability metrics
    axe: bool = False                  # run axe-core accessibility audit (needs js_render, internet)
    wait_ms: int = 500
    custom_js: str = ""                # JS snippet run in every rendered page; `return` a JSON value
    custom_search: list = field(default_factory=list)   # [{name,type:contains|not_contains|regex,value,scope:html|text}]
    extractions: list = field(default_factory=list)     # [{name,type:css|xpath|regex,expr,attr}]
    max_text_chars: int = 30000

    @classmethod
    def from_dict(cls, d: dict | None) -> "CrawlConfig":
        d = d or {}
        known = {f.name for f in fields(cls)}
        c = cls(**{k: v for k, v in d.items() if k in known})
        c.max_pages = min(max(int(c.max_pages), 1), 100_000)
        c.concurrency = min(max(int(c.concurrency), 1), 50)
        return c


# ---------------------------------------------------------------- data model
@dataclass
class Page:
    url: str
    status: int = 0
    final_url: str = ""
    load_ms: int = 0
    size_bytes: int = 0
    title: str = ""
    title_count: int = 0
    meta_description: str = ""
    meta_desc_count: int = 0
    meta_keywords: str = ""
    robots_meta: str = ""
    x_robots: str = ""
    canonical: str = ""
    canonical_count: int = 0
    canonical_header: str = ""
    lang: str = ""
    viewport: bool = False
    viewport_content: str = ""
    charset: str = ""
    meta_refresh: str = ""
    amp_url: str = ""
    rel_next: str = ""
    rel_prev: str = ""
    hreflang: list = field(default_factory=list)       # [[lang, href], ...]
    h1: list = field(default_factory=list)
    h2: list = field(default_factory=list)
    headings: list = field(default_factory=list)       # [[level, text], ...]
    headings_order: list = field(default_factory=list)
    word_count: int = 0
    raw_word_count: int = 0                            # words before JS rendering
    raw_link_count: int = 0
    text: str = ""
    text_ratio: float = 0.0
    content_hash: str = ""
    html_hash: str = ""
    flesch: float = 0.0
    images: int = 0
    images_missing_alt: int = 0
    image_list: list = field(default_factory=list)     # [{src,alt,w,h,lazy}]
    links: list = field(default_factory=list)          # [[url, anchor, nofollow(0/1), internal(0/1)], ...]
    internal_links: list = field(default_factory=list)
    external_links: list = field(default_factory=list)
    resources: list = field(default_factory=list)      # [[url, kind], ...]
    mixed_content: list = field(default_factory=list)
    forms: int = 0
    insecure_forms: int = 0
    og: dict = field(default_factory=dict)
    twitter_card: bool = False
    json_ld: list = field(default_factory=list)
    microdata: list = field(default_factory=list)      # [{type, props}]
    schema_types: list = field(default_factory=list)
    has_author: bool = False
    has_date: bool = False
    question_headings: int = 0
    lists_and_tables: int = 0
    a11y: dict = field(default_factory=dict)
    mobile_static: dict = field(default_factory=dict)
    mobile_render: dict = field(default_factory=dict)
    axe: list = field(default_factory=list)
    custom_search: dict = field(default_factory=dict)
    extractions: dict = field(default_factory=dict)
    js_result: object = None
    js_failed_resources: list = field(default_factory=list)
    js_console_errors: int = 0
    headers: dict = field(default_factory=dict)
    redirect_hops: list = field(default_factory=list)  # [[url, status], ...] (every URL that redirected)
    redirect_final: str = ""
    redirect_final_status: int = 0
    redirect_issue: str = ""                           # loop | too_many
    depth: int = 0
    blocked_by_robots: bool = False
    in_sitemap: bool = False
    is_html: bool = True
    error: str = ""


@dataclass
class SiteData:
    base: str
    pages: dict = field(default_factory=dict)
    robots_txt: str | None = None
    sitemap_urls: list = field(default_factory=list)
    sitemap_found: bool = False
    sitemaps: list = field(default_factory=list)         # [{url,type,urls,bytes,status}]
    sitemap_meta: dict = field(default_factory=dict)     # url -> {lastmod, alts, sitemap}
    llms_txt: str | None = None
    broken_links: list = field(default_factory=list)     # (source, target, status) – rebuilt in finalize()
    redirect_chains: list = field(default_factory=list)  # (start, final)
    resources: dict = field(default_factory=dict)        # url -> {status,size,kind,ctype,refs}
    external_status: dict = field(default_factory=dict)  # url -> status (0 = unreachable)
    config: dict = field(default_factory=dict)
    started: float = 0.0
    finished: float = 0.0
    # derived caches (not persisted)
    derived: dict = field(default_factory=dict)


# ---------------------------------------------------------------- helpers
def canon(u: str) -> str:
    """Defragment and give bare hosts a trailing slash so '/' and '' are the same page."""
    u = urldefrag(u)[0]
    p = urlparse(u)
    return u if p.path else u + "/" if not p.query else u.replace("?", "/?", 1)


def normalize_base(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    u = urlparse(url)
    return f"{u.scheme}://{u.netloc}"


def _root(host: str) -> str:
    host = host.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def _schema_types(node) -> list[str]:
    out: list[str] = []
    if isinstance(node, list):
        for n in node:
            out += _schema_types(n)
    elif isinstance(node, dict):
        t = node.get("@type")
        if isinstance(t, str):
            out.append(t)
        elif isinstance(t, list):
            out += [x for x in t if isinstance(x, str)]
        for v in node.values():
            if isinstance(v, (dict, list)):
                out += _schema_types(v)
    return out


def _syllables(w: str) -> int:
    return max(1, len(re.findall(r"[aeiouy]+", w.lower())))


def flesch(text: str) -> float:
    words = re.findall(r"[A-Za-z']+", text)
    sents = max(1, len(re.findall(r"[.!?]+(\s|$)", text)))
    if len(words) < 30:
        return 0.0
    syl = sum(_syllables(w) for w in words)
    return round(206.835 - 1.015 * len(words) / sents - 84.6 * syl / len(words), 1)


def _link_header_alternates(link_header: str) -> tuple[list, str]:
    """Parse hreflang alternates and canonical out of an HTTP Link header."""
    alts, canon_url = [], ""
    for part in re.split(r",\s*(?=<)", link_header or ""):
        m = re.match(r'\s*<([^>]+)>(.*)', part)
        if not m:
            continue
        href, params = m.group(1), m.group(2)
        if re.search(r'rel="?canonical"?', params):
            canon_url = href
        hl = re.search(r'hreflang="?([\w-]+)"?', params)
        if hl and re.search(r'rel="?alternate"?', params):
            alts.append([hl.group(1), href])
    return alts, canon_url


# ---------------------------------------------------------------- HTML parsing
def fill_html(p: Page, html: str, base_url: str, cfg: CrawlConfig, is_https: bool) -> None:
    soup = BeautifulSoup(html, "lxml")
    host_root = _root(urlparse(base_url).netloc)

    def internal(host: str) -> bool:
        r = _root(host)
        return r == host_root or (cfg.crawl_subdomains and r.endswith("." + host_root))

    titles = soup.find_all("title")
    p.title_count = len(titles)
    p.title = titles[0].get_text(" ", strip=True) if titles else ""
    descs = soup.find_all("meta", attrs={"name": lambda v: v and v.lower() == "description"})
    p.meta_desc_count = len(descs)
    p.meta_description = (descs[0].get("content") or "").strip() if descs else ""
    kw = soup.find("meta", attrs={"name": lambda v: v and v.lower() == "keywords"})
    p.meta_keywords = (kw.get("content") or "").strip() if kw else ""
    robots = [(m.get("content") or "").lower() for m in soup.find_all(
        "meta", attrs={"name": lambda v: v and v.lower() in ("robots", "googlebot")})]
    p.robots_meta = ", ".join(r for r in robots if r)
    cans = soup.find_all("link", rel=lambda v: v and "canonical" in (v if isinstance(v, list) else [v]))
    p.canonical_count = len(cans)
    p.canonical = (cans[0].get("href") or "").strip() if cans else ""
    p.lang = (soup.html.get("lang") or "") if soup.html else ""
    vp = soup.find("meta", attrs={"name": lambda v: v and v.lower() == "viewport"})
    p.viewport = bool(vp)
    p.viewport_content = (vp.get("content") or "").lower() if vp else ""
    cs = soup.find("meta", charset=True)
    p.charset = (cs.get("charset") if cs else "") or ""
    mr = soup.find("meta", attrs={"http-equiv": lambda v: v and v.lower() == "refresh"})
    p.meta_refresh = (mr.get("content") or "") if mr else ""
    amp = soup.find("link", rel="amphtml")
    p.amp_url = urljoin(base_url, amp["href"]) if amp and amp.get("href") else ""
    for rel in ("next", "prev"):
        t = soup.find("link", rel=rel)
        if t and t.get("href"):
            setattr(p, "rel_" + rel, urljoin(base_url, t["href"]))
    for t in soup.find_all("link", rel="alternate", hreflang=True):
        if t.get("href"):
            p.hreflang.append([t["hreflang"].strip(), urljoin(base_url, t["href"].strip())])

    for tag in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        txt = tag.get_text(" ", strip=True)
        lvl = int(tag.name[1])
        p.headings_order.append(lvl)
        if len(p.headings) < 80:
            p.headings.append([lvl, txt[:200]])
        if lvl == 1:
            p.h1.append(txt)
        elif lvl == 2:
            p.h2.append(txt)
        if lvl >= 2 and (txt.endswith("?") or txt.lower().startswith(("how ", "what ", "why ", "when ", "can ", "is ", "does "))):
            p.question_headings += 1

    for m in soup.find_all("meta"):
        prop = (m.get("property") or "").lower()
        if prop.startswith("og:"):
            p.og[prop] = m.get("content", "")
        if (m.get("name") or "").lower().startswith("twitter:card"):
            p.twitter_card = True

    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or s.get_text() or "")
            p.json_ld.append(data)
            p.schema_types += _schema_types(data)
        except Exception:
            p.schema_types.append("__invalid_json_ld__")
    for el in soup.find_all(attrs={"itemscope": True, "itemtype": True}):
        props = sorted({x.get("itemprop") for x in el.find_all(attrs={"itemprop": True}) if x.get("itemprop")})
        t = (el.get("itemtype") or "").rstrip("/").split("/")[-1]
        p.microdata.append({"type": t, "props": props})
        p.schema_types.append(t)
    for el in soup.find_all(attrs={"typeof": True}):
        p.schema_types.append(el["typeof"].split(":")[-1])

    p.has_author = bool(
        soup.find("meta", attrs={"name": "author"}) or soup.find(attrs={"rel": "author"})
        or soup.find(class_=lambda c: c and "author" in c.lower() if isinstance(c, str) else False)
        or "Person" in p.schema_types)
    p.has_date = bool(soup.find("time") or soup.find("meta", attrs={"property": "article:modified_time"})
                      or soup.find("meta", attrs={"property": "article:published_time"})
                      or any(k in p.schema_types for k in ("Article", "BlogPosting", "NewsArticle")))

    # images
    imgs = soup.find_all("img")
    p.images = len(imgs)
    p.images_missing_alt = sum(1 for i in imgs if not (i.get("alt") or "").strip())
    for i in imgs[:200]:
        src = i.get("src") or i.get("data-src") or ""
        if src and not src.startswith("data:"):
            p.image_list.append({"src": urljoin(base_url, src), "alt": i.get("alt"), "w": i.get("width"),
                                 "h": i.get("height"), "lazy": (i.get("loading") or "") == "lazy",
                                 "srcset": bool(i.get("srcset"))})
            p.resources.append([urljoin(base_url, src), "image"])

    # links
    seen_i, seen_e = set(), set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#", "sms:", "data:")) or not href:
            continue
        full = canon(urljoin(base_url, href))
        if not full.startswith(("http://", "https://")):
            continue
        rel = a.get("rel") or []
        nofollow = int(any(r in ("nofollow", "ugc", "sponsored") for r in rel))
        anchor = a.get_text(" ", strip=True)
        if not anchor:
            im = a.find("img")
            anchor = ((im.get("alt") if im else "") or a.get("aria-label") or a.get("title") or "").strip()
        is_int = internal(urlparse(full).netloc)
        if len(p.links) < 2000:
            p.links.append([full, anchor[:120], nofollow, int(is_int)])
        if is_int:
            if full not in seen_i:
                seen_i.add(full)
                p.internal_links.append(full)
        elif full not in seen_e:
            seen_e.add(full)
            p.external_links.append(full)

    # resources + mixed content
    for t in soup.find_all("link", rel=lambda v: v and "stylesheet" in (v if isinstance(v, list) else [v])):
        if t.get("href"):
            p.resources.append([urljoin(base_url, t["href"]), "css"])
    for t in soup.find_all("script", src=True):
        p.resources.append([urljoin(base_url, t["src"]), "js"])
    if is_https:
        for u, kind in p.resources:
            if u.startswith("http://"):
                p.mixed_content.append(u)
        for t in soup.find_all(["iframe", "video", "audio", "source", "embed"], src=True):
            if urljoin(base_url, t["src"]).startswith("http://"):
                p.mixed_content.append(urljoin(base_url, t["src"]))
    forms = soup.find_all("form")
    p.forms = len(forms)
    p.insecure_forms = sum(1 for f in forms if urljoin(base_url, f.get("action") or "").startswith("http://") and is_https)

    _accessibility(p, soup)
    _mobile_static(p, soup)
    _custom(p, html, soup, cfg)

    p.lists_and_tables = len(soup.find_all(["ul", "ol", "table"]))
    for t in soup(["script", "style", "noscript", "nav", "footer", "head", "template"]):
        t.decompose()
    text = soup.get_text(" ", strip=True)
    p.word_count = len(text.split())
    p.text = text[:cfg.max_text_chars]
    p.text_ratio = round(len(text) / max(len(html), 1) * 100, 1)
    norm = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
    p.content_hash = hashlib.md5(norm.encode()).hexdigest() if norm else ""
    p.flesch = flesch(text)


def _accessibility(p: Page, soup) -> None:
    a: dict = {}
    labelled = {l.get("for") for l in soup.find_all("label") if l.get("for")}
    bad_inputs = 0
    for i in soup.find_all(["input", "select", "textarea"]):
        if i.get("type") in ("hidden", "submit", "button", "image", "reset"):
            continue
        if not (i.get("id") in labelled or i.get("aria-label") or i.get("aria-labelledby") or i.get("title")
                or i.find_parent("label")):
            bad_inputs += 1
    a["input_no_label"] = bad_inputs
    a["button_no_name"] = sum(1 for b in soup.find_all("button")
                              if not (b.get_text(strip=True) or b.get("aria-label") or b.get("title") or b.get("aria-labelledby")))
    a["link_no_name"] = sum(1 for l in soup.find_all("a", href=True)
                            if not (l.get_text(strip=True) or l.get("aria-label") or l.get("title") or l.find("img", alt=True)))
    a["empty_heading"] = sum(1 for h in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]) if not h.get_text(strip=True))
    a["iframe_no_title"] = sum(1 for f in soup.find_all("iframe") if not f.get("title"))
    ids = [t["id"] for t in soup.find_all(id=True)]
    a["duplicate_ids"] = len(ids) - len(set(ids))
    a["tabindex_positive"] = sum(1 for t in soup.find_all(attrs={"tabindex": True}) if str(t["tabindex"]).lstrip("-").isdigit() and int(t["tabindex"]) > 0)
    a["table_no_headers"] = sum(1 for t in soup.find_all("table") if not t.find("th"))
    a["autoplay_media"] = sum(1 for m in soup.find_all(["video", "audio"]) if m.has_attr("autoplay"))
    a["zoom_disabled"] = int("user-scalable=no" in p.viewport_content.replace(" ", "") or
                             re.search(r"maximum-scale=1(\.0)?\b", p.viewport_content) is not None)
    a["no_main_landmark"] = int(not (soup.find("main") or soup.find(attrs={"role": "main"})))
    first = soup.find("a", href=True)
    a["no_skip_link"] = int(not (first and (first["href"].startswith("#")) ))
    a["no_lang"] = int(not p.lang)
    a["img_missing_alt_attr"] = sum(1 for i in soup.find_all("img") if i.get("alt") is None)
    p.a11y = a


def _mobile_static(p: Page, soup) -> None:
    m = {"viewport_ok": "width=device-width" in p.viewport_content.replace(" ", "") if p.viewport else False}
    big = 0
    small_font = 0
    for t in soup.find_all(style=True):
        st = t["style"].lower().replace(" ", "")
        w = re.search(r"(?:^|;)width:(\d+)px", st)
        if w and int(w.group(1)) > 480:
            big += 1
        f = re.search(r"font-size:(\d+(?:\.\d+)?)px", st)
        if f and float(f.group(1)) < 12:
            small_font += 1
    m["fixed_width_elements"] = big
    m["small_font_inline"] = small_font
    m["plugins"] = len(soup.find_all(["embed", "object", "applet"]))
    m["fixed_viewport_width"] = bool(re.search(r"width=\d{3,4}", p.viewport_content.replace(" ", "")))
    p.mobile_static = m


def _custom(p: Page, html: str, soup, cfg: CrawlConfig) -> None:
    text_cache = None
    for rule in cfg.custom_search:
        name, typ, val = rule.get("name") or rule.get("value", ""), rule.get("type", "contains"), rule.get("value", "")
        if not val:
            continue
        if rule.get("scope") == "text":
            if text_cache is None:
                text_cache = soup.get_text(" ", strip=True)
            hay = text_cache
        else:
            hay = html
        try:
            n = len(re.findall(val, hay, re.I)) if typ == "regex" else hay.lower().count(val.lower())
        except re.error:
            n = 0
        p.custom_search[name] = n if typ != "not_contains" else int(n == 0)
    if not cfg.extractions:
        return
    tree = None
    for ex in cfg.extractions:
        name, typ, expr, attr = ex.get("name", ""), ex.get("type", "css"), ex.get("expr", ""), ex.get("attr", "text")
        if not expr:
            continue
        out: list[str] = []
        try:
            if typ == "css":
                for el in soup.select(expr)[:20]:
                    out.append(el.get_text(" ", strip=True) if attr in ("", "text") else
                               str(el) if attr == "html" else (el.get(attr) or ""))
            elif typ == "xpath":
                from lxml import html as lhtml
                tree = tree if tree is not None else lhtml.fromstring(html)
                for el in tree.xpath(expr)[:20]:
                    out.append(str(el) if isinstance(el, str) else el.text_content().strip() if attr in ("", "text")
                               else (el.get(attr) or ""))
            elif typ == "regex":
                for m in re.finditer(expr, html, re.I | re.S):
                    out.append((m.group(1) if m.groups() else m.group(0)).strip())
                    if len(out) >= 20:
                        break
        except Exception as e:  # bad expression: report it instead of failing the crawl
            out = [f"ERROR: {e}"[:100]]
        p.extractions[name] = out


def parse_page(url: str, resp: httpx.Response, load_ms: int, cfg: CrawlConfig | None = None) -> Page:
    cfg = cfg or CrawlConfig()
    p = Page(url=url, status=resp.status_code, final_url=str(resp.url), load_ms=load_ms, size_bytes=len(resp.content))
    p.headers = {k: resp.headers[k][:300] for k in KEEP_HEADERS if k in resp.headers}
    p.x_robots = resp.headers.get("x-robots-tag", "").lower()
    alts, can_h = _link_header_alternates(resp.headers.get("link", ""))
    p.hreflang += alts
    p.canonical_header = can_h
    ctype = resp.headers.get("content-type", "")
    if "html" not in ctype:
        p.is_html = False
        return p
    p.html_hash = hashlib.md5(resp.content).hexdigest()
    fill_html(p, resp.text, str(resp.url), cfg, str(resp.url).startswith("https://"))
    return p


# ---------------------------------------------------------------- JS rendering (optional: playwright)
MOBILE_JS = """() => {
  const vw = innerWidth, out = {viewport_width: vw, overflow_x: document.documentElement.scrollWidth > vw + 1, small_text: 0, small_tap_targets: 0, tap_targets: 0};
  for (const el of document.querySelectorAll('body *')) {
    if (el.children.length === 0 && el.textContent.trim().length > 3) { const fs = parseFloat(getComputedStyle(el).fontSize); if (fs && fs < 12) out.small_text++; }
  }
  for (const el of document.querySelectorAll('a[href],button,input:not([type=hidden]),select,textarea,[role=button]')) {
    const r = el.getBoundingClientRect(); if (r.width === 0 || r.height === 0) continue; out.tap_targets++;
    if (r.width < 44 || r.height < 44) out.small_tap_targets++;
  }
  return out; }"""


class Renderer:
    def __init__(self, cfg: CrawlConfig, cookies: list[dict]):
        self.cfg, self.cookies = cfg, cookies
        self.pw = self.browser = self.ctx = None

    async def start(self):
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise RuntimeError("JavaScript rendering needs Playwright: pip install playwright && playwright install chromium")
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch()
        kw: dict = {"user_agent": self.cfg.user_agent}
        if self.cfg.mobile:
            kw.update(viewport={"width": 375, "height": 667}, is_mobile=True, has_touch=True, device_scale_factor=2)
        else:
            kw.update(viewport={"width": 1366, "height": 768})
        basic = (self.cfg.auth or {}).get("basic")
        if basic:
            kw["http_credentials"] = {"username": basic.get("user", ""), "password": basic.get("password", "")}
        if (self.cfg.auth or {}).get("headers"):
            kw["extra_http_headers"] = self.cfg.auth["headers"]
        self.ctx = await self.browser.new_context(**kw)
        if self.cookies:
            await self.ctx.add_cookies(self.cookies)

    async def stop(self):
        for o in (self.ctx, self.browser):
            try:
                if o:
                    await o.close()
            except Exception:
                pass
        if self.pw:
            await self.pw.stop()

    async def render(self, url: str) -> dict:
        page = await self.ctx.new_page()
        failed: list[str] = []
        errors = [0]
        page.on("requestfailed", lambda r: failed.append(r.url))
        page.on("console", lambda m: errors.__setitem__(0, errors[0] + (m.type == "error")))
        out: dict = {}
        try:
            await page.goto(url, wait_until="networkidle", timeout=self.cfg.timeout * 2000)
            await page.wait_for_timeout(self.cfg.wait_ms)
            out["html"] = await page.content()
            out["final_url"] = page.url
            if self.cfg.mobile:
                out["mobile"] = await page.evaluate(MOBILE_JS)
            if self.cfg.custom_js.strip():
                try:
                    out["js_result"] = await page.evaluate("(async()=>{" + self.cfg.custom_js + "\n})()")
                except Exception as e:
                    out["js_result"] = f"ERROR: {str(e)[:200]}"
            if self.cfg.axe:
                try:
                    await page.add_script_tag(url=AXE_CDN)
                    out["axe"] = await page.evaluate(
                        "axe.run().then(r=>r.violations.map(v=>({id:v.id,impact:v.impact,help:v.help,nodes:v.nodes.length})))")
                except Exception as e:
                    out["axe"] = [{"id": "axe_error", "impact": "n/a", "help": str(e)[:150], "nodes": 0}]
            out["failed"] = failed[:20]
            out["console_errors"] = errors[0]
        finally:
            await page.close()
        return out


# ---------------------------------------------------------------- sitemaps / robots
async def _get(client: httpx.AsyncClient, url: str) -> httpx.Response | None:
    try:
        r = await client.get(url)
        return r if r.status_code == 200 else None
    except Exception:
        return None


async def _get_text(client: httpx.AsyncClient, url: str) -> str | None:
    r = await _get(client, url)
    return r.text if r else None


def _xml_of(r: httpx.Response) -> str:
    b = r.content
    if b[:2] == b"\x1f\x8b":
        try:
            b = gzip.decompress(b)
        except Exception:
            pass
    return b.decode("utf-8", "replace")


async def _read_sitemaps(client, site: SiteData, roots: list[str]) -> None:
    todo, seen = [(u, 0) for u in roots], set()
    while todo and len(seen) < 60:
        url, depth = todo.pop(0)
        if url in seen:
            continue
        seen.add(url)
        r = await _get(client, url)
        if not r:
            site.sitemaps.append({"url": url, "type": "unreachable", "urls": 0, "bytes": 0, "status": 0})
            continue
        xml = _xml_of(r)
        soup = BeautifulSoup(xml, "lxml-xml")
        if soup.find("sitemapindex"):
            kids = [l.get_text(strip=True) for l in soup.find_all("loc")]
            site.sitemaps.append({"url": url, "type": "index", "urls": len(kids), "bytes": len(r.content), "status": 200})
            if depth < 3:
                todo += [(k, depth + 1) for k in kids]
            continue
        entries = soup.find_all("url")
        for e in entries:
            loc = e.find("loc")
            if not loc:
                continue
            u = canon(loc.get_text(strip=True))
            site.sitemap_urls.append(u)
            lm = e.find("lastmod")
            alts = [[l.get("hreflang"), l.get("href")] for l in e.find_all(["xhtml:link", "link"], rel="alternate") if l.get("hreflang")]
            site.sitemap_meta[u] = {"lastmod": lm.get_text(strip=True) if lm else "", "alts": alts, "sitemap": url}
        if entries:
            site.sitemap_found = True
        site.sitemaps.append({"url": url, "type": "urlset", "urls": len(entries), "bytes": len(r.content), "status": 200})


# ---------------------------------------------------------------- main crawl
async def _build_client(cfg: CrawlConfig) -> httpx.AsyncClient:
    auth_cfg = cfg.auth or {}
    auth = None
    if auth_cfg.get("basic"):
        auth = httpx.BasicAuth(auth_cfg["basic"].get("user", ""), auth_cfg["basic"].get("password", ""))
    elif auth_cfg.get("digest"):
        auth = httpx.DigestAuth(auth_cfg["digest"].get("user", ""), auth_cfg["digest"].get("password", ""))
    headers = {"User-Agent": cfg.user_agent, **(auth_cfg.get("headers") or {})}
    client = httpx.AsyncClient(headers=headers, follow_redirects=False, timeout=cfg.timeout, verify=cfg.verify_tls,
                               auth=auth, cookies=auth_cfg.get("cookies") or None)
    login = auth_cfg.get("login")
    if login and login.get("url"):  # form-based login: session cookies are kept by the client
        data = dict(login.get("data") or {})
        if login.get("csrf"):
            page = await client.get(login["url"])
            for i in BeautifulSoup(page.text, "lxml").find_all("input", attrs={"type": "hidden"}):
                if i.get("name") and i.get("name") not in data:
                    data[i["name"]] = i.get("value", "")
        r = await client.request(login.get("method", "POST"), login["url"], data=data, follow_redirects=True)
        if r.status_code >= 400:
            raise RuntimeError(f"Login failed: HTTP {r.status_code}")
    return client


async def _request(client: httpx.AsyncClient, url: str, cfg: CrawlConfig):
    """GET following redirects manually so every hop (and loops) are recorded."""
    hops: list[list] = []
    cur, seen = url, {url}
    r = None
    for _ in range(cfg.max_redirects + 1):
        r = await client.get(cur)
        loc = r.headers.get("location")
        if r.status_code in (301, 302, 303, 307, 308) and loc:
            nxt = canon(urljoin(str(r.url), loc))
            hops.append([cur, r.status_code])
            if nxt in seen:
                return r, hops, nxt, "loop"
            seen.add(nxt)
            cur = nxt
            continue
        return r, hops, cur, ""
    return r, hops, cur, "too_many"


async def crawl(start_url: str, max_pages: int | None = None, concurrency: int | None = None, progress=None,
                config: CrawlConfig | dict | None = None) -> SiteData:
    cfg = config if isinstance(config, CrawlConfig) else CrawlConfig.from_dict(config)
    if max_pages:
        cfg.max_pages = min(max(max_pages, 1), 100_000)
    if concurrency:
        cfg.concurrency = concurrency
    base = normalize_base(start_url)
    site = SiteData(base=base, config={k: v for k, v in cfg.__dict__.items() if k != "auth"}, started=time.time())
    inc = [re.compile(x) for x in cfg.include if x]
    exc = [re.compile(x) for x in cfg.exclude if x]
    base_root = _root(urlparse(base).netloc)

    def in_scope(u: str) -> bool:
        h = _root(urlparse(u).netloc)
        if not (h == base_root or (cfg.crawl_subdomains and h.endswith("." + base_root))):
            return False
        if u.lower().split("?")[0].endswith(SKIP_EXT):
            return False
        if inc and not any(r.search(u) for r in inc):
            return False
        return not any(r.search(u) for r in exc)

    client = await _build_client(cfg)
    renderer: Renderer | None = None
    try:
        site.robots_txt = await _get_text(client, base + "/robots.txt")
        site.llms_txt = await _get_text(client, base + "/llms.txt")
        rp = robotparser.RobotFileParser()
        rp.parse((site.robots_txt or "").splitlines())

        sm_roots = [base + "/sitemap.xml"]
        if site.robots_txt:
            sm_roots += re.findall(r"(?im)^sitemap:\s*(\S+)", site.robots_txt)
        await _read_sitemaps(client, site, list(dict.fromkeys(sm_roots)))
        site.sitemap_urls = list(dict.fromkeys(site.sitemap_urls))

        if cfg.js_render:
            cookies = [{"name": c.name, "value": c.value, "domain": c.domain or urlparse(base).hostname, "path": c.path or "/"}
                       for c in client.cookies.jar]
            renderer = Renderer(cfg, cookies)
            await renderer.start()

        start = canon(start_url if start_url.startswith("http") else base)
        queue: list[tuple[str, int]] = [(start, 0)]
        seen = {start}
        sem = asyncio.Semaphore(cfg.concurrency)
        link_sources: dict[str, str] = {}
        if cfg.crawl_sitemap:
            for u in site.sitemap_urls:
                if u not in seen and in_scope(u):
                    seen.add(u)
                    queue.append((u, 1))

        async def fetch(u: str, depth: int) -> Page:
            if cfg.respect_robots and site.robots_txt and not rp.can_fetch(cfg.user_agent, u):
                return Page(url=u, depth=depth, blocked_by_robots=True, is_html=False)
            async with sem:
                if cfg.delay_ms:
                    await asyncio.sleep(cfg.delay_ms / 1000)
                t0 = time.perf_counter()
                try:
                    r, hops, final, issue = await _request(client, u, cfg)
                    ms = int((time.perf_counter() - t0) * 1000)
                    if hops:
                        p = Page(url=u, status=hops[0][1], final_url=final, load_ms=ms, depth=depth, is_html=False,
                                 redirect_hops=hops, redirect_final=final, redirect_final_status=r.status_code,
                                 redirect_issue=issue)
                        p.headers = {"location": r.headers.get("location", "")} if issue else {}
                        return p
                    p = parse_page(u, r, ms, cfg)
                    p.depth = depth
                    if renderer and p.is_html and p.status == 200:
                        p.raw_word_count, p.raw_link_count = p.word_count, len(p.internal_links)
                        try:
                            rr = await renderer.render(u)
                            raw = (p.raw_word_count, p.raw_link_count, p.headers, p.x_robots, p.canonical_header, p.hreflang)
                            fresh = Page(url=u, status=p.status, final_url=p.final_url, load_ms=ms, size_bytes=p.size_bytes,
                                         depth=depth, html_hash=p.html_hash)
                            fill_html(fresh, rr["html"], rr["final_url"], cfg, rr["final_url"].startswith("https://"))
                            fresh.raw_word_count, fresh.raw_link_count, fresh.headers, fresh.x_robots, fresh.canonical_header = raw[:5]
                            fresh.hreflang += [h for h in raw[5] if h not in fresh.hreflang]
                            fresh.mobile_render = rr.get("mobile", {})
                            fresh.axe = rr.get("axe", [])
                            fresh.js_result = rr.get("js_result")
                            fresh.js_failed_resources = rr.get("failed", [])
                            fresh.js_console_errors = rr.get("console_errors", 0)
                            p = fresh
                        except Exception as e:
                            p.error = f"render failed: {str(e)[:150]}"
                    return p
                except Exception as e:
                    return Page(url=u, depth=depth, error=f"{type(e).__name__}: {str(e)[:160]}")

        while queue and len(site.pages) < cfg.max_pages:
            batch = []
            while queue and len(batch) < cfg.concurrency and len(site.pages) + len(batch) < cfg.max_pages:
                batch.append(queue.pop(0))
            results = await asyncio.gather(*(fetch(u, d) for u, d in batch))
            for pg in results:
                site.pages[pg.url] = pg
                if progress:
                    progress(len(site.pages), cfg.max_pages, pg.url)
                nxt: list[str] = []
                if pg.redirect_final and not pg.redirect_issue:
                    nxt.append(pg.redirect_final)
                nxt += [l[0] for l in pg.links if l[3] and not (cfg.respect_nofollow and l[2])]
                if cfg.max_depth and pg.depth >= cfg.max_depth:
                    nxt = [n for n in nxt if n == pg.redirect_final]
                for l in nxt:
                    if l not in seen and in_scope(l):
                        seen.add(l)
                        link_sources[l] = pg.url
                        queue.append((l, pg.depth + (0 if l == pg.redirect_final else 1)))
        site.derived["link_sources"] = link_sources
        sm = set(site.sitemap_urls)
        for pg in site.pages.values():
            pg.in_sitemap = pg.url in sm or pg.url.rstrip("/") in {s.rstrip("/") for s in sm}

        await _check_extras(client, site, cfg, sem)
    finally:
        if renderer:
            await renderer.stop()
        await client.aclose()
    finalize(site)
    site.finished = time.time()
    return site


async def _check_extras(client: httpx.AsyncClient, site: SiteData, cfg: CrawlConfig, sem) -> None:
    """HEAD-check external links and page resources (images/CSS/JS)."""
    async def probe(u: str):
        async with sem:
            try:
                r = await client.head(u, follow_redirects=True)
                if r.status_code in (405, 403, 501) or (r.status_code == 200 and "content-length" not in r.headers):
                    r = await client.get(u, follow_redirects=True)
                return r.status_code, int(r.headers.get("content-length") or len(r.content) or 0), r.headers.get("content-type", "")
            except Exception:
                return 0, 0, ""

    if cfg.check_external:
        ext = list(dict.fromkeys(l for pg in site.pages.values() for l in pg.external_links))[:cfg.max_external]
        for u, res in zip(ext, await asyncio.gather(*(probe(u) for u in ext))):
            site.external_status[u] = res[0]
    if cfg.check_resources:
        refs: dict[str, tuple[str, list[str]]] = {}
        for pg in site.pages.values():
            for u, kind in pg.resources:
                if u.startswith("http"):
                    refs.setdefault(u, (kind, []))[1].append(pg.url)
        urls = list(refs)[:cfg.max_resources]
        for u, res in zip(urls, await asyncio.gather(*(probe(u) for u in urls))):
            kind, pages = refs[u]
            site.resources[u] = {"status": res[0], "size": res[1], "ctype": res[2], "kind": kind, "refs": pages[:5],
                                 "ref_count": len(pages)}


def finalize(site: SiteData) -> None:
    """Rebuild cross-page lists (broken links with every source, redirect chains)."""
    site.broken_links, site.redirect_chains = [], []
    for pg in site.pages.values():
        if pg.redirect_hops:
            site.redirect_chains.append((pg.url, pg.redirect_final))
        for url, _anchor, _nf, internal in pg.links:
            if internal:
                t = site.pages.get(url)
                if t and (t.status >= 400 or t.error):
                    site.broken_links.append((pg.url, url, t.status))
            else:
                st = site.external_status.get(url)
                if st is not None and (st >= 400 or st == 0):
                    site.broken_links.append((pg.url, url, st))
    for url, r in site.resources.items():
        if r["status"] >= 400 or r["status"] == 0:
            for src in r["refs"]:
                site.broken_links.append((src, url, r["status"]))
    ls = site.derived.get("link_sources")
    site.derived.clear()
    if ls:
        site.derived["link_sources"] = ls
