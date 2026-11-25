"""One entry point that crawls, audits, saves and summarises — shared by the web UI, CLI, scheduler and MCP server."""
from __future__ import annotations

import asyncio
import json
from collections import Counter
from pathlib import Path

from . import ai, analysis as an, reports, store
from .audit import Issue, run_technical_audit, score
from .crawler import CrawlConfig, SiteData, crawl
from .geo import run_geo_audit
from .keywords import extract_keywords


def summarize(site: SiteData, tech: list[Issue], geo_issues: list[Issue], geo: dict, deep_note: str = "") -> dict:
    pages = list(site.pages.values())
    ix = Counter(an.indexability(site, p)[0] for p in pages)
    codes = Counter(f"{p.status // 100}xx" if p.status else ("blocked" if p.blocked_by_robots else "no response") for p in pages)
    sc = score(tech, len(an.html_pages(site)))
    return {"site": site.base, "pages_crawled": len(pages), "score": sc, "geo": geo, "issues": [i.to_dict() for i in tech + geo_issues],
            "keywords": extract_keywords(site), "ai_enabled": ai.ai_available(), "indexability": dict(ix), "status_codes": dict(codes),
            "depth": an.depth_histogram(site), "duration_s": round(site.finished - site.started, 1) if site.finished else None,
            "js_rendered": bool(site.config.get("js_render")), "sitemaps": len(site.sitemaps), "note": deep_note}


async def run_full(url: str, config: dict | CrawlConfig | None = None, progress=None, label: str = "", deep: bool = True) -> tuple[dict, SiteData, list[Issue]]:
    cfg = config if isinstance(config, CrawlConfig) else CrawlConfig.from_dict(config)
    site = await crawl(url, progress=progress, config=cfg)
    tech, (geo_issues, geo) = await asyncio.gather(asyncio.to_thread(run_technical_audit, site, deep), asyncio.to_thread(run_geo_audit, site))
    all_issues = tech + geo_issues
    result = summarize(site, tech, geo_issues, geo)
    result["crawl_id"] = result["audit_id"] = await asyncio.to_thread(store.save_crawl, site, all_issues, result, label)
    return result, site, all_issues


def export_files(crawl_id: int, out_dir: str, formats: list[str], names: list[str] | None = None) -> list[str]:
    """Write exports (csv/xlsx/json) for a saved crawl; returns file paths."""
    loaded = store.load_crawl(crawl_id)
    if not loaded:
        raise KeyError("crawl not found")
    site, issues, summary = loaded
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    tag = f"crawl{crawl_id}"
    if "xlsx" in formats:
        f = out / f"{tag}.xlsx"
        f.write_bytes(reports.full_workbook(site, issues, names))
        written.append(str(f))
    if "csv" in formats:
        tables = {"issues": reports.issues_table(issues)}
        for n in names or ["internal", "response_codes", "redirects", "broken_links", "titles", "descriptions", "directives", "hreflang",
                           "duplicates_exact", "duplicates_near", "structured", "images", "security", "accessibility"]:
            tables[n] = reports.get(site, n)
        for n, (cols, rows) in tables.items():
            f = out / f"{tag}_{n}.csv"
            f.write_text(reports.to_csv(cols, rows), encoding="utf-8-sig")
            written.append(str(f))
    if "json" in formats:
        f = out / f"{tag}.json"
        f.write_text(json.dumps({"summary": summary, "issues": [i.to_dict(full=True) for i in issues]}, indent=1, default=str), encoding="utf-8")
        written.append(str(f))
    return written
