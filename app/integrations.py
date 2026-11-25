"""External data sources: Google Analytics 4, Search Console (pages + URL Inspection), PageSpeed Insights, first-party traffic.

Auth: GA_ACCESS_TOKEN / GSC_ACCESS_TOKEN (short-lived OAuth bearer), or set GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET +
GOOGLE_REFRESH_TOKEN and tokens are refreshed automatically. GA also needs GA_PROPERTY_ID. PSI works without a key at low quota
(PSI_API_KEY raises it).
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import date, timedelta
from urllib.parse import quote, urlparse

import httpx

from . import store, traffic
from .crawler import SiteData

_TOKENS: dict[str, tuple[str, float]] = {}
SETTABLE = {"GA_ACCESS_TOKEN", "GSC_ACCESS_TOKEN", "GA_PROPERTY_ID", "PSI_API_KEY", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET",
            "GOOGLE_REFRESH_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "OLLAMA_URL", "OLLAMA_MODEL",
            "SEO_AI_PROVIDER", "SEO_AI_MODEL", "DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD", "SMTP_HOST", "SMTP_PORT", "SMTP_USER",
            "SMTP_PASSWORD", "SMTP_FROM", "LANGUAGETOOL_URL"}
SECRET = {"GA_ACCESS_TOKEN", "GSC_ACCESS_TOKEN", "PSI_API_KEY", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN", "ANTHROPIC_API_KEY",
          "OPENAI_API_KEY", "GEMINI_API_KEY", "DATAFORSEO_PASSWORD", "SMTP_PASSWORD"}


def set_env(values: dict[str, str]) -> list[str]:
    """Set allow-listed settings for this server process only (never written to disk)."""
    done = []
    for k, v in values.items():
        if k in SETTABLE:
            if v:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)
            _TOKENS.clear()
            done.append(k)
    return done


def settings_view() -> dict[str, str]:
    return {k: ("•••• set" if k in SECRET and os.getenv(k) else os.getenv(k, "")) for k in sorted(SETTABLE)}


async def google_token(kind: str) -> str:
    """kind: 'ga' | 'gsc'. Returns a bearer token, refreshing via refresh-token when configured."""
    direct = os.getenv("GA_ACCESS_TOKEN" if kind == "ga" else "GSC_ACCESS_TOKEN")
    if direct:
        return direct
    cid, cs, rt = os.getenv("GOOGLE_CLIENT_ID"), os.getenv("GOOGLE_CLIENT_SECRET"), os.getenv("GOOGLE_REFRESH_TOKEN")
    if not (cid and cs and rt):
        raise RuntimeError(("GA_ACCESS_TOKEN + GA_PROPERTY_ID" if kind == "ga" else "GSC_ACCESS_TOKEN") +
                           " not set (or GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET/GOOGLE_REFRESH_TOKEN for auto-refresh)")
    tok = _TOKENS.get("google")
    if tok and tok[1] > time.time() + 60:
        return tok[0]
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post("https://oauth2.googleapis.com/token", data={"client_id": cid, "client_secret": cs, "refresh_token": rt,
                                                                      "grant_type": "refresh_token"})
        r.raise_for_status()
        j = r.json()
    _TOKENS["google"] = (j["access_token"], time.time() + int(j.get("expires_in", 3600)))
    return j["access_token"]


def ga_configured() -> bool:
    return bool(os.getenv("GA_PROPERTY_ID") and (os.getenv("GA_ACCESS_TOKEN") or os.getenv("GOOGLE_REFRESH_TOKEN")))


def gsc_configured() -> bool:
    return bool(os.getenv("GSC_ACCESS_TOKEN") or os.getenv("GOOGLE_REFRESH_TOKEN"))


def _path(u: str) -> str:
    p = urlparse(u)
    return (p.path.rstrip("/") or "/").lower()


# ------------------------------------------------------------------ Google Analytics 4
async def ga_pages(days: int = 28, limit: int = 1000, property_id: str = "") -> list[dict]:
    pid = property_id or os.getenv("GA_PROPERTY_ID", "")
    if not pid:
        raise RuntimeError("GA_PROPERTY_ID not set")
    token = await google_token("ga")
    async with httpx.AsyncClient(timeout=60, headers={"Authorization": f"Bearer {token}"}) as c:
        r = await c.post(f"https://analyticsdata.googleapis.com/v1beta/properties/{pid}:runReport", json={
            "dateRanges": [{"startDate": f"{days}daysAgo", "endDate": "yesterday"}], "dimensions": [{"name": "pagePath"}],
            "metrics": [{"name": "screenPageViews"}, {"name": "sessions"}, {"name": "totalUsers"}, {"name": "engagementRate"},
                        {"name": "averageSessionDuration"}, {"name": "conversions"}],
            "orderBys": [{"metric": {"metricName": "screenPageViews"}, "desc": True}], "limit": limit})
        r.raise_for_status()
    out = []
    for row in r.json().get("rows", []):
        m = [float(x["value"]) for x in row["metricValues"]]
        out.append({"path": row["dimensionValues"][0]["value"], "views": int(m[0]), "sessions": int(m[1]), "users": int(m[2]),
                    "engagement_rate": round(m[3] * 100, 1), "avg_session_s": round(m[4]), "conversions": int(m[5])})
    return out


# ------------------------------------------------------------------ Search Console
async def gsc_pages(site_url: str, days: int = 28, limit: int = 1000) -> list[dict]:
    token = await google_token("gsc")
    end = date.today() - timedelta(days=2)
    async with httpx.AsyncClient(timeout=60, headers={"Authorization": f"Bearer {token}"}) as c:
        r = await c.post(f"https://www.googleapis.com/webmasters/v3/sites/{quote(site_url, safe='')}/searchAnalytics/query",
                         json={"startDate": (end - timedelta(days=days)).isoformat(), "endDate": end.isoformat(),
                               "dimensions": ["page"], "rowLimit": limit})
        r.raise_for_status()
    return [{"url": x["keys"][0], "clicks": x["clicks"], "impressions": x["impressions"], "ctr": round(x["ctr"] * 100, 2),
             "position": round(x["position"], 1)} for x in r.json().get("rows", [])]


async def gsc_inspect(site_url: str, url: str) -> dict:
    """Google's index status for one URL (URL Inspection API)."""
    token = await google_token("gsc")
    async with httpx.AsyncClient(timeout=60, headers={"Authorization": f"Bearer {token}"}) as c:
        r = await c.post("https://searchconsole.googleapis.com/v1/urlInspection/index:inspect",
                         json={"inspectionUrl": url, "siteUrl": site_url})
        r.raise_for_status()
    res = r.json().get("inspectionResult", {})
    ix = res.get("indexStatusResult", {})
    return {"url": url, "verdict": ix.get("verdict"), "coverage": ix.get("coverageState"), "robots_txt": ix.get("robotsTxtState"),
            "indexing": ix.get("indexingState"), "last_crawl": ix.get("lastCrawlTime"), "google_canonical": ix.get("googleCanonical"),
            "user_canonical": ix.get("userCanonical"), "crawled_as": ix.get("crawledAs"), "mobile": res.get("mobileUsabilityResult", {}).get("verdict"),
            "rich_results": res.get("richResultsResult", {}).get("verdict")}


async def gsc_inspect_many(site_url: str, urls: list[str], concurrency: int = 3) -> list[dict]:
    sem = asyncio.Semaphore(concurrency)

    async def one(u):
        async with sem:
            try:
                return await gsc_inspect(site_url, u)
            except Exception as e:
                return {"url": u, "verdict": "ERROR", "coverage": str(e)[:120]}
    return await asyncio.gather(*(one(u) for u in urls))


# ------------------------------------------------------------------ PageSpeed Insights
def _metric(audits: dict, key: str) -> float | None:
    a = audits.get(key) or {}
    return a.get("numericValue")


async def psi(url: str, strategy: str = "mobile") -> dict:
    params = [("url", url), ("strategy", strategy)] + [("category", c) for c in ("performance", "accessibility", "best-practices", "seo")]
    if os.getenv("PSI_API_KEY"):
        params.append(("key", os.environ["PSI_API_KEY"]))
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.get("https://www.googleapis.com/pagespeedonline/v5/runPagespeed", params=params)
        if r.status_code == 429:
            raise RuntimeError("PageSpeed Insights quota exceeded — set PSI_API_KEY for a higher quota")
        r.raise_for_status()
    j = r.json()
    lh = j.get("lighthouseResult", {})
    cats = {k: round((v.get("score") or 0) * 100) for k, v in lh.get("categories", {}).items()}
    au = lh.get("audits", {})
    opps = sorted(({"title": a["title"], "savings_ms": round(a.get("details", {}).get("overallSavingsMs", 0)),
                    "savings_kb": round(a.get("details", {}).get("overallSavingsBytes", 0) / 1024)}
                   for a in au.values() if a.get("details", {}).get("type") == "opportunity" and a.get("details", {}).get("overallSavingsMs", 0) > 0),
                  key=lambda x: -x["savings_ms"])[:8]
    field_ = {}
    for k, v in (j.get("loadingExperience", {}).get("metrics") or {}).items():
        field_[k.replace("_", " ").lower()] = {"p75": v.get("percentile"), "rating": v.get("category")}
    out = {"url": url, "strategy": strategy, "scores": cats,
           "lab": {"fcp_ms": _metric(au, "first-contentful-paint"), "lcp_ms": _metric(au, "largest-contentful-paint"),
                   "cls": _metric(au, "cumulative-layout-shift"), "tbt_ms": _metric(au, "total-blocking-time"),
                   "speed_index_ms": _metric(au, "speed-index"), "tti_ms": _metric(au, "interactive")},
           "field": field_, "opportunities": opps, "overall_field_rating": j.get("loadingExperience", {}).get("overall_category")}
    store.save_psi(url, strategy, out)
    return out


async def psi_many(urls: list[str], strategy: str = "mobile", concurrency: int = 2) -> list[dict]:
    sem = asyncio.Semaphore(concurrency)

    async def one(u):
        async with sem:
            try:
                return await psi(u, strategy)
            except Exception as e:
                return {"url": u, "strategy": strategy, "error": str(e)[:150]}
    return await asyncio.gather(*(one(u) for u in urls))


# ------------------------------------------------------------------ merge analytics into the crawl
async def enrich(site: SiteData, days: int = 28, sources: tuple[str, ...] = ("traffic", "gsc", "ga")) -> dict:
    """Per-page rows combining crawl facts with first-party traffic, GSC and GA4. Also flags mismatches."""
    from . import analysis as an
    host = urlparse(site.base).netloc.replace("www.", "")
    data: dict[str, dict] = {}
    notes: list[str] = []
    if "traffic" in sources:
        try:
            st = await asyncio.to_thread(traffic.stats, host, days)
            for r in st["top_pages"]:
                data.setdefault(r["path"].split("?")[0].rstrip("/").lower() or "/", {})["fp_views"] = r["views"]
        except Exception as e:
            notes.append(f"traffic: {e}")
    if "gsc" in sources:
        try:
            for r in await gsc_pages(site.base + "/", days):
                d = data.setdefault(_path(r["url"]), {})
                d["gsc_clicks"] = d.get("gsc_clicks", 0) + r["clicks"]
                d["gsc_impressions"] = d.get("gsc_impressions", 0) + r["impressions"]
                d["gsc_position"] = r["position"]
        except Exception as e:
            notes.append(f"gsc: {e}")
    if "ga" in sources:
        try:
            for r in await ga_pages(days):
                d = data.setdefault(r["path"].split("?")[0].rstrip("/").lower() or "/", {})
                d["ga_views"] = d.get("ga_views", 0) + r["views"]
                d["ga_sessions"] = d.get("ga_sessions", 0) + r["sessions"]
                d["ga_engagement"] = r["engagement_rate"]
        except Exception as e:
            notes.append(f"ga: {e}")
    rows, crawled_paths = [], set()
    for p in an.html_pages(site):
        k = _path(p.url)
        crawled_paths.add(k)
        d = data.get(k, {})
        status, reason = an.indexability(site, p)
        flags = []
        if status == "Non-Indexable" and (d.get("gsc_clicks") or d.get("ga_sessions")):
            flags.append("Non-indexable page receiving traffic")
        if status == "Indexable" and "gsc" in sources and not d.get("gsc_impressions") and not any(n.startswith("gsc") for n in notes):
            flags.append("Indexable but no Google impressions")
        if (d.get("gsc_impressions", 0) > 100) and d.get("gsc_clicks", 0) / max(d["gsc_impressions"], 1) < 0.01:
            flags.append("High impressions, very low CTR")
        if (d.get("gsc_clicks") or d.get("ga_sessions")) and not an.inlinks(site).get(p.url) and p.depth > 0:
            flags.append("Orphan page with traffic")
        rows.append({"url": p.url, "indexability": status, "words": p.word_count, "depth": p.depth, **d, "flags": "; ".join(flags)})
    missing = [{"path": k, **v} for k, v in data.items() if k not in crawled_paths and (v.get("ga_sessions") or v.get("gsc_clicks"))]
    return {"rows": rows, "not_crawled": sorted(missing, key=lambda r: -(r.get("gsc_clicks", 0) + r.get("ga_sessions", 0)))[:200], "notes": notes}
