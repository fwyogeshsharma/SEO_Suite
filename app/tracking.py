"""SQLite storage + rank/backlink providers.

Providers are pluggable. Implemented:
  - DataForSEO (SERP rank + backlink summary)  -> env DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD
  - Google Search Console (real clicks/impressions/avg position) -> env GSC_ACCESS_TOKEN (OAuth bearer token)
Without credentials the endpoints return a clear "not configured" message instead of fake data.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import httpx

DB_PATH = Path(os.getenv("SEO_DB", Path(__file__).parent.parent / "seo.db"))


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript("""
    CREATE TABLE IF NOT EXISTS audits(id INTEGER PRIMARY KEY, site TEXT, created REAL, score INTEGER, geo_score INTEGER, data TEXT);
    CREATE TABLE IF NOT EXISTS ranks(id INTEGER PRIMARY KEY, site TEXT, keyword TEXT, position INTEGER, url TEXT, checked REAL);
    CREATE TABLE IF NOT EXISTS tracked(site TEXT, keyword TEXT, PRIMARY KEY(site, keyword));
    """)
    return con


def save_audit(site: str, score: int, geo_score: int, data: dict) -> int:
    with db() as con:
        cur = con.execute("INSERT INTO audits(site,created,score,geo_score,data) VALUES(?,?,?,?,?)",
                          (site, time.time(), score, geo_score, json.dumps(data)))
        return cur.lastrowid


def audit_history(site: str) -> list[dict]:
    with db() as con:
        rows = con.execute("SELECT id,created,score,geo_score FROM audits WHERE site=? ORDER BY created", (site,)).fetchall()
    return [dict(r) for r in rows]


def get_audit(audit_id: int) -> dict | None:
    with db() as con:
        r = con.execute("SELECT data FROM audits WHERE id=?", (audit_id,)).fetchone()
    return json.loads(r["data"]) if r else None


def track_keywords(site: str, keywords: list[str]):
    with db() as con:
        con.executemany("INSERT OR IGNORE INTO tracked(site,keyword) VALUES(?,?)", [(site, k) for k in keywords])


def tracked(site: str) -> list[str]:
    with db() as con:
        return [r["keyword"] for r in con.execute("SELECT keyword FROM tracked WHERE site=?", (site,))]


def rank_history(site: str) -> list[dict]:
    with db() as con:
        return [dict(r) for r in con.execute(
            "SELECT keyword,position,url,checked FROM ranks WHERE site=? ORDER BY checked", (site,))]


def dataforseo_configured() -> bool:
    return bool(os.getenv("DATAFORSEO_LOGIN") and os.getenv("DATAFORSEO_PASSWORD"))


def gsc_configured() -> bool:
    return bool(os.getenv("GSC_ACCESS_TOKEN") or os.getenv("GOOGLE_REFRESH_TOKEN"))


def _dfs_auth():
    return (os.environ["DATAFORSEO_LOGIN"], os.environ["DATAFORSEO_PASSWORD"])


async def check_ranks(site_host: str, keywords: list[str], location_code: int = 2840, language_code: str = "en") -> list[dict]:
    """Live Google SERP check via DataForSEO; stores the first position where site_host appears (top 100)."""
    if not dataforseo_configured():
        raise RuntimeError("DataForSEO credentials not set (DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD)")
    results = []
    async with httpx.AsyncClient(timeout=60, auth=_dfs_auth()) as client:
        for kw in keywords:
            r = await client.post("https://api.dataforseo.com/v3/serp/google/organic/live/advanced",
                                  json=[{"keyword": kw, "location_code": location_code,
                                         "language_code": language_code, "depth": 100}])
            r.raise_for_status()
            items = (r.json().get("tasks") or [{}])[0].get("result") or [{}]
            items = items[0].get("items") or []
            pos, url = None, None
            for it in items:
                if it.get("type") == "organic" and site_host in (it.get("domain") or ""):
                    pos, url = it.get("rank_absolute"), it.get("url")
                    break
            results.append({"keyword": kw, "position": pos, "url": url})
            with db() as con:
                con.execute("INSERT INTO ranks(site,keyword,position,url,checked) VALUES(?,?,?,?,?)",
                            (site_host, kw, pos, url, time.time()))
    return results


async def backlink_summary(site_host: str) -> dict:
    if not dataforseo_configured():
        raise RuntimeError("DataForSEO credentials not set (DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD)")
    async with httpx.AsyncClient(timeout=60, auth=_dfs_auth()) as client:
        r = await client.post("https://api.dataforseo.com/v3/backlinks/summary/live",
                              json=[{"target": site_host, "include_subdomains": True}])
        r.raise_for_status()
        res = ((r.json().get("tasks") or [{}])[0].get("result") or [{}])[0]
    keys = ["backlinks", "referring_domains", "referring_main_domains", "rank", "broken_backlinks",
            "referring_ips", "backlinks_spam_score"]
    return {k: res.get(k) for k in keys}


async def gsc_queries(site_url: str, days: int = 28, limit: int = 50) -> list[dict]:
    """Real query data from Google Search Console (site_url as registered, e.g. https://example.com/ or sc-domain:example.com)."""
    from .integrations import google_token  # supports auto-refresh via GOOGLE_REFRESH_TOKEN
    token = await google_token("gsc")
    from datetime import date, timedelta
    end = date.today() - timedelta(days=2)
    start = end - timedelta(days=days)
    from urllib.parse import quote
    async with httpx.AsyncClient(timeout=30, headers={"Authorization": f"Bearer {token}"}) as client:
        r = await client.post(
            f"https://www.googleapis.com/webmasters/v3/sites/{quote(site_url, safe='')}/searchAnalytics/query",
            json={"startDate": start.isoformat(), "endDate": end.isoformat(), "dimensions": ["query"], "rowLimit": limit})
        r.raise_for_status()
        return [{"query": row["keys"][0], "clicks": row["clicks"], "impressions": row["impressions"],
                 "ctr": round(row["ctr"] * 100, 2), "position": round(row["position"], 1)}
                for row in r.json().get("rows", [])]
