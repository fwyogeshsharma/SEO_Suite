"""Technical SEO checks. Each issue: severity (critical/warning/info), category, message, urls."""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from urllib.parse import urlparse

from . import analysis as an
from .crawler import Page, SiteData
from .structured import validate_page


@dataclass
class Issue:
    id: str
    severity: str  # critical | warning | info
    category: str
    message: str
    urls: list[str] = field(default_factory=list)
    fix_hint: str = ""

    def to_dict(self, full: bool = False):
        return {"id": self.id, "severity": self.severity, "category": self.category,
                "message": self.message, "urls": self.urls if full else self.urls[:25], "count": len(self.urls),
                "fix_hint": self.fix_hint}


WEIGHT = {"critical": 6, "warning": 3, "info": 1}
GENERIC_ANCHORS = {"click here", "here", "read more", "more", "learn more", "link", "this", "this link", "click", "details"}


def _star_group_blocks(robots: str) -> bool:
    in_star, last_agent = False, False
    for raw in robots.splitlines():
        line = raw.split("#")[0].strip()
        if ":" not in line:
            continue
        k, v = (x.strip().lower() for x in line.split(":", 1))
        if k == "user-agent":
            in_star = (v == "*") if not last_agent else (in_star or v == "*")
            last_agent = True
        else:
            last_agent = False
            if in_star and k == "disallow" and v == "/":
                return True
    return False


def _html_ok(site: SiteData) -> list[Page]:
    return an.html_pages(site)


def run_technical_audit(site: SiteData, deep: bool = True) -> list[Issue]:
    issues: list[Issue] = []
    pages = _html_ok(site)
    allp = list(site.pages.values())
    ix = {p.url: an.indexability(site, p) for p in allp}

    def add(id_, sev, cat, msg, urls=None, hint=""):
        if urls is None or urls:
            issues.append(Issue(id_, sev, cat, msg, urls or [], hint))

    # ---------------------------------------------------------------- site level
    if not site.base.startswith("https://"):
        add("no_https", "critical", "security", "Site is not served over HTTPS", [site.base],
            "Install a TLS certificate and 301-redirect all HTTP traffic to HTTPS.")
    if site.robots_txt is None:
        add("no_robots", "warning", "technical", "robots.txt not found", [site.base + "/robots.txt"],
            "Add a robots.txt that allows crawling and references your sitemap.")
    elif _star_group_blocks(site.robots_txt):
        add("robots_blocks_all", "critical", "technical", "robots.txt blocks the whole site (Disallow: / for User-agent: *)",
            [site.base + "/robots.txt"], "Remove 'Disallow: /' for User-agent: * unless intentional.")
    if not site.sitemap_found:
        add("no_sitemap", "warning", "sitemap", "No XML sitemap found", [site.base + "/sitemap.xml"],
            "Generate sitemap.xml (Sitemaps tab) and list it in robots.txt.")

    # ---------------------------------------------------------------- response codes
    add("client_error", "critical", "links", "Internal URLs returning 4xx client errors",
        [p.url for p in allp if 400 <= p.status < 500], "Restore the page, 301 to a relevant URL, or remove links to it.")
    add("server_error", "critical", "links", "Internal URLs returning 5xx server errors",
        [p.url for p in allp if p.status >= 500], "Fix the server error; check logs and capacity.")
    add("no_response", "critical", "links", "URLs with no response (timeout / DNS / TLS error)",
        [f"{p.url} ({p.error})" for p in allp if p.error and not p.blocked_by_robots])
    add("blocked_robots", "info", "technical", "URLs blocked by robots.txt", [p.url for p in allp if p.blocked_by_robots],
        "Confirm these are meant to be blocked. Blocked pages can't pass signals and can't show noindex.")
    int_broken = [f"{s} → {t} [{c}]" for s, t, c in site.broken_links if urlparse(t).netloc.replace("www.", "") == urlparse(site.base).netloc.replace("www.", "")
                  and t not in site.resources]
    ext_broken = [f"{s} → {t} [{c}]" for s, t, c in site.broken_links if t in site.external_status]
    res_broken = [f"{s} → {t} [{c}]" for s, t, c in site.broken_links if t in site.resources]
    add("broken_links", "critical", "links", "Broken internal links (pointing to 4xx/5xx/unreachable pages)", int_broken,
        "Update the link to a working URL or remove it.")
    add("broken_external", "warning", "links", "Broken external links", ext_broken, "Replace or remove dead outbound links.")
    add("broken_resources", "warning", "links", "Broken images / CSS / JS files", res_broken, "Fix paths or restore the missing files.")

    # ---------------------------------------------------------------- redirects
    redirecting = [p for p in allp if p.redirect_hops]
    add("redirects", "info", "redirects", "URLs that redirect", [f"{p.url} → {p.redirect_final} [{p.status}]" for p in redirecting],
        "Link directly to final URLs.")
    add("redirect_chain", "warning", "redirects", "Redirect chains (more than one hop)",
        [f"{p.url} → … → {p.redirect_final} ({len(p.redirect_hops)} hops)" for p in redirecting if len(p.redirect_hops) > 1 and not p.redirect_issue],
        "Point the first URL straight at the final destination.")
    add("redirect_loop", "critical", "redirects", "Redirect loops / too many redirects",
        [p.url for p in redirecting if p.redirect_issue], "Fix the redirect rules so they terminate.")
    add("redirect_to_error", "critical", "redirects", "Redirects ending in an error",
        [f"{p.url} → {p.redirect_final} [{p.redirect_final_status}]" for p in redirecting if p.redirect_final_status >= 400 and not p.redirect_issue])
    add("redirect_temporary", "info", "redirects", "Temporary redirects (302/307) — use 301/308 if permanent",
        [p.url for p in redirecting if p.status in (302, 307)])
    add("meta_refresh", "warning", "redirects", "Meta refresh redirects", [p.url for p in pages if "url=" in p.meta_refresh.lower()],
        "Replace meta refresh with a server-side 301.")
    red_targets = {p.url for p in redirecting}
    add("links_to_redirect", "info", "redirects", "Pages linking to URLs that redirect",
        [f"{p.url} → {l[0]}" for p in pages for l in p.links if l[3] and l[0] in red_targets][:2000],
        "Update internal links to the final URL.")

    # ---------------------------------------------------------------- titles & meta
    by_title, by_desc, by_h1 = defaultdict(list), defaultdict(list), defaultdict(list)
    for p in pages:
        by_title[p.title.strip().lower()].append(p.url)
        by_desc[p.meta_description.strip().lower()].append(p.url)
        if p.h1:
            by_h1[p.h1[0].strip().lower()].append(p.url)
    add("missing_title", "critical", "technical", "Pages missing <title>", [p.url for p in pages if not p.title],
        "Add a unique, descriptive title (30–60 chars) with the primary keyword.")
    add("title_short", "warning", "technical", "Title too short (<30 chars)", [p.url for p in pages if p.title and len(p.title) < 30],
        "Expand the title to 30–60 characters.")
    add("title_long", "warning", "technical", "Title too long (>60 chars, may be truncated in SERPs)", [p.url for p in pages if len(p.title) > 60],
        "Trim the title to ≤60 characters.")
    add("title_pixels", "info", "technical", "Title wider than ~580px (likely truncated)",
        [p.url for p in pages if p.title and an.pixel_width(p.title) > 580 and len(p.title) <= 60], "Shorten or use narrower words.")
    add("multi_title", "warning", "technical", "Pages with more than one <title>", [p.url for p in pages if p.title_count > 1])
    add("dup_title", "warning", "duplicates", "Duplicate titles", [u for t, us in by_title.items() if t and len(us) > 1 for u in us],
        "Give each page a unique title.")
    add("title_same_h1", "info", "technical", "Title identical to H1", [p.url for p in pages if p.title and p.h1 and p.title.strip().lower() == p.h1[0].strip().lower()],
        "Differentiate title (SERP) from H1 (page) to target more phrases.")
    add("missing_desc", "warning", "technical", "Pages missing meta description", [p.url for p in pages if not p.meta_description],
        "Write a 70–160 char description with a call to action.")
    add("desc_short", "info", "technical", "Meta description too short (<70 chars)",
        [p.url for p in pages if p.meta_description and len(p.meta_description) < 70])
    add("desc_long", "info", "technical", "Meta description too long (>160 chars)", [p.url for p in pages if len(p.meta_description) > 160],
        "Trim to ≤160 characters.")
    add("multi_desc", "warning", "technical", "Pages with multiple meta descriptions", [p.url for p in pages if p.meta_desc_count > 1])
    add("dup_desc", "info", "duplicates", "Duplicate meta descriptions", [u for t, us in by_desc.items() if t and len(us) > 1 for u in us],
        "Write unique descriptions per page.")
    add("meta_keywords", "info", "technical", "Meta keywords tag present (ignored by Google/Bing)", [p.url for p in pages if p.meta_keywords])
    add("missing_h1", "critical", "content", "Pages missing H1", [p.url for p in pages if not p.h1], "Add exactly one H1 that states the page topic.")
    add("multi_h1", "warning", "content", "Pages with multiple H1s", [p.url for p in pages if len(p.h1) > 1], "Keep one H1; demote others to H2.")
    add("h1_long", "info", "content", "H1 longer than 70 characters", [p.url for p in pages if p.h1 and len(p.h1[0]) > 70])
    add("dup_h1", "warning", "duplicates", "Duplicate H1s", [u for t, us in by_h1.items() if t and len(us) > 1 for u in us])
    add("missing_h2", "info", "content", "Pages with no H2 headings", [p.url for p in pages if not p.h2 and p.word_count > 300])
    add("heading_skip", "info", "content", "Heading levels skipped (e.g. H2 → H4)",
        [p.url for p in pages if any(b - a > 1 for a, b in zip(p.headings_order, p.headings_order[1:]))], "Keep heading hierarchy sequential.")
    add("empty_heading", "warning", "content", "Empty heading tags", [p.url for p in pages if p.a11y.get("empty_heading")])

    # ---------------------------------------------------------------- content
    add("thin_content", "warning", "content", "Thin content (<300 words)", [p.url for p in pages if p.word_count < 300],
        "Expand with useful, original content that satisfies search intent.")
    add("low_text_ratio", "info", "content", "Low text-to-HTML ratio (<10%)", [p.url for p in pages if p.text_ratio < 10 and p.word_count >= 100])
    add("hard_to_read", "info", "content", "Hard to read (Flesch reading ease < 30)", [p.url for p in pages if p.flesch and p.flesch < 30],
        "Use shorter sentences and simpler words.")
    add("js_content", "warning", "javascript", "Content or links appear only after JavaScript rendering",
        [p.url for p in pages if p.raw_word_count and (p.word_count > p.raw_word_count * 1.5 + 50 or len(p.internal_links) > p.raw_link_count + 5)],
        "Server-render critical content/links so crawlers and AI bots that don't run JS can see them.")
    add("js_failed_resources", "info", "javascript", "Resources that failed to load during rendering", [p.url for p in pages if p.js_failed_resources])
    add("js_console_errors", "info", "javascript", "JavaScript console errors during rendering", [p.url for p in pages if p.js_console_errors])
    add("img_alt", "warning", "images", "Images missing alt text", [p.url for p in pages if p.images_missing_alt],
        "Add descriptive alt text to every meaningful image.")
    add("img_alt_long", "info", "images", "Alt text longer than 100 characters",
        [p.url for p in pages if any(len(i.get("alt") or "") > 100 for i in p.image_list)])
    add("img_no_dimensions", "info", "images", "Images without width/height (causes layout shift)",
        [p.url for p in pages if any(not (i.get("w") and i.get("h")) for i in p.image_list)], "Set width and height attributes.")
    big_imgs = [f"{u} ({r['size'] // 1024} KB)" for u, r in site.resources.items() if r["kind"] == "image" and r["size"] > 200_000]
    add("img_large", "warning", "images", "Images larger than 200 KB", big_imgs, "Compress and serve responsive/modern formats (WebP/AVIF).")
    add("img_legacy_format", "info", "images", "Images served as JPEG/PNG (consider WebP/AVIF)",
        [u for u, r in site.resources.items() if r["kind"] == "image" and re.search(r"\.(jpe?g|png)(\?|$)", u, re.I) and r["size"] > 50_000])

    # ---------------------------------------------------------------- directives & canonicals
    add("noindex", "warning", "directives", "Pages set to noindex", [p.url for p in pages if "noindex" in p.robots_meta],
        "Confirm noindex is intentional.")
    add("x_robots_noindex", "warning", "directives", "Pages noindexed via X-Robots-Tag header", [p.url for p in allp if "noindex" in p.x_robots])
    add("nofollow_meta", "info", "directives", "Pages with a page-level nofollow directive",
        [p.url for p in pages if "nofollow" in p.robots_meta or "nofollow" in p.x_robots])
    add("restrictive_snippets", "info", "directives", "Pages restricting snippets (nosnippet / max-snippet:0)",
        [p.url for p in pages if re.search(r"nosnippet|max-snippet:\s*0", p.robots_meta + p.x_robots)])
    add("noindex_canonical", "warning", "directives", "Conflicting directives: noindex page with a canonical to another URL",
        [p.url for p in pages if ix[p.url][1] == "noindex" and an.canonical_status(site, p) == "Canonicalised"])
    add("missing_canonical", "info", "directives", "Pages without canonical tag", [p.url for p in pages if an.canonical_status(site, p) == "Missing"],
        "Add <link rel=\"canonical\"> pointing to the preferred URL.")
    add("multi_canonical", "warning", "directives", "Pages with multiple canonical tags", [p.url for p in pages if p.canonical_count > 1])
    add("canonicalised", "info", "directives", "Canonicalised pages (canonical points elsewhere)",
        [f"{p.url} → {an.canonical_target(p)}" for p in pages if an.canonical_status(site, p) == "Canonicalised"])
    by_url = {an.norm_url(p.url): p for p in allp}
    bad_can = []
    for p in pages:
        if an.canonical_status(site, p) == "Canonicalised":
            t = by_url.get(an.norm_url(an.canonical_target(p)))
            if t and (t.status != 200 or t.redirect_hops or "noindex" in t.robots_meta):
                bad_can.append(f"{p.url} → {t.url} [{t.status}{' noindex' if 'noindex' in t.robots_meta else ''}]")
    add("canonical_to_bad_target", "critical", "directives", "Canonical points to a redirected, errored or noindex URL", bad_can)
    add("canonical_relative", "info", "directives", "Relative canonical URLs", [p.url for p in pages if p.canonical and not p.canonical.startswith("http")])
    add("canonical_header_mismatch", "warning", "directives", "HTML canonical differs from HTTP header canonical",
        [p.url for p in pages if p.canonical and p.canonical_header and an.norm_url(p.canonical) != an.norm_url(p.canonical_header)])
    pag_bad = []
    for p in pages:
        for rel, u in (("next", p.rel_next), ("prev", p.rel_prev)):
            t = by_url.get(an.norm_url(u)) if u else None
            if t and t.status != 200:
                pag_bad.append(f"{p.url} rel={rel} → {u} [{t.status}]")
    add("pagination_broken", "warning", "directives", "rel=next/prev pointing to non-200 pages", pag_bad)

    # ---------------------------------------------------------------- hreflang
    HL = {
        "hreflang_no_self": ("warning", "Hreflang: page missing self-referencing entry"),
        "hreflang_no_return": ("critical", "Hreflang: missing return (reciprocal) links"),
        "hreflang_bad_code": ("critical", "Hreflang: invalid language/region code"),
        "hreflang_dup_lang": ("warning", "Hreflang: multiple entries for the same language"),
        "hreflang_bad_target": ("critical", "Hreflang: points to non-200 / redirected URLs"),
        "hreflang_noindex_target": ("warning", "Hreflang: points to noindex pages"),
        "hreflang_canonical_conflict": ("warning", "Hreflang: page canonicalised elsewhere"),
        "hreflang_no_xdefault": ("info", "Hreflang: no x-default entry"),
        "hreflang_lang_mismatch": ("info", "Hreflang: differs from <html lang>"),
    }
    for iid, urls in an.hreflang_findings(site).items():
        sev, msg = HL[iid]
        add(iid, sev, "hreflang", msg, urls, "See Google's hreflang guidelines: every alternate must link back, codes must be ISO 639-1 (+ ISO 3166-1 region).")

    # ---------------------------------------------------------------- duplicates & similarity
    exact = an.exact_duplicates(site)
    add("dup_content", "warning", "duplicates", "Exact duplicate pages (identical content)",
        [f"{u}  (group of {len(g['urls'])})" for g in exact if g["type"] == "content" for u in g["urls"]],
        "Consolidate with 301 redirects or canonical tags.")
    if deep and len(pages) <= 3000:
        near = an.near_duplicates(site, 0.9)
        exact_pairs = {frozenset(g["urls"][:2]) for g in exact}
        add("near_dup", "warning", "duplicates", "Near-duplicate pages (≥90% similar)",
            [f"{n['a']} ≈ {n['b']} ({n['similarity']}%)" for n in near if frozenset((n["a"], n["b"])) not in exact_pairs],
            "Differentiate the content or canonicalise to one version.")
        low = an.low_relevance(site)
        add("low_relevance", "info", "content", "Pages with low relevance to the rest of the site's topic",
            [f"{r['url']} ({r['relevance']}%)" for r in low])
        sp = an.spelling_grammar(site)
        add("spelling", "info", "content", "Possible spelling mistakes", [f"{r['url']}: " + ", ".join(s["word"] for s in r["spelling"][:6]) for r in sp if r["spelling"]],
            "Review the flagged words — brand names and jargon can be false positives.")
        add("grammar", "info", "content", "Possible grammar issues", [f"{r['url']}: {r['grammar'][0]['issue']}" for r in sp if r["grammar"]])

    # ---------------------------------------------------------------- structured data
    sd_err, sd_warn = [], []
    for p in pages:
        for r in validate_page(p):
            if r["errors"]:
                sd_err.append(f"{p.url} [{r['type']}] " + "; ".join(r["errors"][:3]))
            elif r["warnings"] and r["rich"]:
                sd_warn.append(f"{p.url} [{r['type']}] " + "; ".join(r["warnings"][:3]))
    add("bad_jsonld", "warning", "structured", "Invalid JSON-LD structured data",
        [p.url for p in pages if "__invalid_json_ld__" in p.schema_types], "Fix JSON syntax in your ld+json blocks.")
    add("sd_errors", "critical", "structured", "Structured data validation errors (missing required properties)", sd_err,
        "Add the required properties so the page is eligible for rich results.")
    add("sd_warnings", "info", "structured", "Structured data missing recommended properties", sd_warn)
    add("no_schema", "warning", "structured", "No structured data (schema.org)", [p.url for p in pages if not p.schema_types],
        "Add JSON-LD (Organization, WebSite, Article, Product, FAQPage as relevant).")

    # ---------------------------------------------------------------- mobile
    add("no_viewport", "critical", "mobile", "Missing mobile viewport meta tag", [p.url for p in pages if not p.viewport],
        "Add <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">.")
    add("viewport_not_responsive", "warning", "mobile", "Viewport tag doesn't use width=device-width",
        [p.url for p in pages if p.viewport and not p.mobile_static.get("viewport_ok")])
    add("mobile_zoom_disabled", "warning", "mobile", "Pinch-zoom disabled via viewport", [p.url for p in pages if p.a11y.get("zoom_disabled")],
        "Remove user-scalable=no / maximum-scale=1.")
    add("mobile_fixed_width", "warning", "mobile", "Fixed-width elements wider than a phone screen",
        [p.url for p in pages if p.mobile_static.get("fixed_width_elements") or p.mobile_static.get("fixed_viewport_width")])
    add("mobile_plugins", "warning", "mobile", "Uses plugins (embed/object) unsupported on mobile", [p.url for p in pages if p.mobile_static.get("plugins")])
    add("mobile_small_font", "info", "mobile", "Text smaller than 12px (inline styles)", [p.url for p in pages if p.mobile_static.get("small_font_inline")])
    add("mobile_overflow", "warning", "mobile", "Content wider than the screen on mobile (horizontal scroll)",
        [p.url for p in pages if p.mobile_render.get("overflow_x")])
    add("mobile_small_tap", "warning", "mobile", "Tap targets smaller than 44×44 px",
        [f"{p.url} ({p.mobile_render['small_tap_targets']}/{p.mobile_render['tap_targets']})" for p in pages
         if p.mobile_render.get("small_tap_targets", 0) > 3])
    add("mobile_small_text", "info", "mobile", "Rendered text smaller than 12px", [p.url for p in pages if p.mobile_render.get("small_text", 0) > 3])
    add("no_lang", "info", "accessibility", "Missing <html lang>", [p.url for p in pages if not p.lang], "Set the lang attribute, e.g. <html lang=\"en\">.")

    # ---------------------------------------------------------------- accessibility
    A11Y = [
        ("input_no_label", "critical", "Form fields without an accessible label"), ("button_no_name", "critical", "Buttons without an accessible name"),
        ("link_no_name", "critical", "Links without an accessible name (empty anchor text)"), ("iframe_no_title", "warning", "iframes without a title"),
        ("duplicate_ids", "warning", "Duplicate element IDs"), ("tabindex_positive", "warning", "Positive tabindex values (breaks focus order)"),
        ("table_no_headers", "warning", "Data tables without header cells"), ("autoplay_media", "warning", "Autoplaying audio/video"),
        ("no_main_landmark", "info", "No <main> landmark"), ("no_skip_link", "info", "No 'skip to content' link"),
        ("img_missing_alt_attr", "critical", "Images missing the alt attribute entirely"),
    ]
    for key, sev, msg in A11Y:
        add("a11y_" + key, sev, "accessibility", "Accessibility: " + msg, [p.url for p in pages if p.a11y.get(key)],
            "See WCAG 2.2 success criteria 1.1.1, 1.3.1, 2.4.1, 4.1.2.")
    axe_by: dict[str, list[str]] = defaultdict(list)
    for p in pages:
        for v in p.axe:
            axe_by[f"{v['id']}|{v['impact']}|{v['help']}"].append(f"{p.url} ({v['nodes']} elements)")
    for k, urls in axe_by.items():
        vid, impact, help_ = k.split("|", 2)
        add("axe_" + vid, {"critical": "critical", "serious": "critical", "moderate": "warning"}.get(impact, "info"), "accessibility",
            f"axe-core: {help_}", urls, "Run axe DevTools for exact element selectors.")

    # ---------------------------------------------------------------- security & performance
    https_pages = [p for p in pages if p.url.startswith("https://")]
    if https_pages and all("strict-transport-security" not in p.headers for p in https_pages):
        add("no_hsts", "warning", "security", "Missing HSTS header (Strict-Transport-Security)", [https_pages[0].url],
            "Send Strict-Transport-Security: max-age=31536000; includeSubDomains on HTTPS responses.")
    add("no_xcto", "info", "security", "Missing X-Content-Type-Options: nosniff", [p.url for p in pages if "x-content-type-options" not in p.headers])
    add("no_clickjack", "info", "security", "No clickjacking protection (X-Frame-Options / CSP frame-ancestors)",
        [p.url for p in pages if "x-frame-options" not in p.headers and "frame-ancestors" not in p.headers.get("content-security-policy", "")])
    add("no_referrer_policy", "info", "security", "Missing Referrer-Policy header", [p.url for p in pages if "referrer-policy" not in p.headers])
    add("mixed_content", "critical", "security", "Mixed content: HTTP resources on HTTPS pages",
        [f"{p.url} → {p.mixed_content[0]}" for p in pages if p.mixed_content], "Load every resource over HTTPS.")
    add("insecure_form", "critical", "security", "Forms posting to HTTP", [p.url for p in pages if p.insecure_forms])
    add("http_internal_links", "warning", "security", "Internal links to HTTP URLs from HTTPS pages",
        [f"{p.url} → {l[0]}" for p in https_pages for l in p.links if l[3] and l[0].startswith("http://")][:2000])
    add("slow", "warning", "performance", "Slow server response (>1500 ms)", [p.url for p in pages if p.load_ms > 1500],
        "Use caching/CDN, optimize backend and compress responses.")
    add("heavy", "info", "performance", "Large HTML document (>500 KB)", [p.url for p in pages if p.size_bytes > 500_000], "Reduce markup, lazy-load, split content.")
    add("no_compression", "info", "performance", "HTML served without compression (gzip/br)",
        [p.url for p in pages if p.size_bytes > 10_000 and not p.headers.get("content-encoding")])
    add("deep_pages", "info", "links", "Pages more than 4 clicks from the start page", [p.url for p in pages if p.depth > 4],
        "Link important pages closer to the home page.")

    # ---------------------------------------------------------------- social
    add("no_og", "info", "social", "Missing Open Graph tags (og:title/og:description/og:image)",
        [p.url for p in pages if not all(k in p.og for k in ("og:title", "og:description", "og:image"))], "Add Open Graph tags so shares look good.")
    add("no_twitter", "info", "social", "Missing Twitter Card tag", [p.url for p in pages if not p.twitter_card],
        "Add <meta name=\"twitter:card\" content=\"summary_large_image\">.")

    # ---------------------------------------------------------------- internal linking
    inl = an.inlinks(site)
    start = next(iter(site.pages), None)
    add("orphan", "warning", "links", "Pages with no internal links pointing to them",
        [p.url for p in pages if not inl.get(p.url) and p.url != start and p.url != site.base + "/"],
        "Link to these pages from relevant content or navigation.")
    add("nofollow_internal", "info", "links", "Internal links marked nofollow",
        [f"{p.url} → {l[0]}" for p in pages for l in p.links if l[3] and l[2]][:2000])
    add("empty_anchor", "info", "links", "Internal links with empty anchor text",
        [f"{p.url} → {l[0]}" for p in pages for l in p.links if l[3] and not l[1]][:2000], "Use descriptive anchor text.")
    add("generic_anchor", "info", "links", "Non-descriptive anchor text ('click here', 'read more')",
        [f"{p.url} → {l[0]} \"{l[1]}\"" for p in pages for l in p.links if l[3] and l[1].lower() in GENERIC_ANCHORS][:2000])
    add("too_many_links", "info", "links", "Pages with more than 300 links", [p.url for p in pages if len(p.links) > 300])
    add("no_outlinks", "info", "links", "Pages with no internal outlinks (dead ends)", [p.url for p in pages if not p.internal_links and len(pages) > 1])

    # ---------------------------------------------------------------- URLs
    url_by: dict[str, list[str]] = defaultdict(list)
    for p in pages:
        for prob in an.url_problems(p.url):
            url_by[prob].append(p.url)
    for prob, urls in url_by.items():
        add("url_" + re.sub(r"\W+", "_", prob.lower())[:30], "info", "urls", "URL: " + prob, urls)

    # ---------------------------------------------------------------- sitemaps
    SM = {"sitemap_redirect": ("warning", "Sitemap contains redirecting URLs"), "sitemap_error": ("critical", "Sitemap contains 4xx/5xx URLs"),
          "sitemap_noindex": ("warning", "Sitemap contains noindex URLs"), "sitemap_non_canonical": ("warning", "Sitemap contains non-canonical URLs"),
          "sitemap_blocked": ("warning", "Sitemap contains URLs blocked by robots.txt"),
          "indexable_not_in_sitemap": ("info", "Indexable pages missing from the sitemap"), "sitemap_too_large": ("critical", "Sitemap exceeds 50,000 URLs / 50 MB")}
    for iid, urls in an.sitemap_findings(site).items():
        sev, msg = SM[iid]
        add(iid, sev, "sitemap", msg, urls, "Sitemaps should list only indexable, canonical, 200-status URLs.")
    return issues


def score(issues: list[Issue], n_pages: int) -> dict:
    """0–100 score. Penalty scales with share of affected pages, so big sites aren't punished unfairly."""
    n = max(n_pages, 1)
    penalty = 0.0
    by_cat: dict[str, float] = defaultdict(float)
    for i in issues:
        share = min(1.0, len(i.urls) / n) if i.urls else 1.0
        pts = WEIGHT[i.severity] * (0.4 + 0.6 * share)
        penalty += pts
        by_cat[i.category] += pts
    total = max(0, round(100 - penalty * 0.9))
    return {"overall": total, "by_category": {k: max(0, round(100 - v * 4)) for k, v in by_cat.items()}}
