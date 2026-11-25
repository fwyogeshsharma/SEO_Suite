"""MCP server (stdio, JSON-RPC 2.0, protocol 2024-11-05) exposing the SEO Suite to AI assistants.

Add to Claude Desktop / Claude Code, e.g.:
  claude mcp add seo-suite -- python -m app.cli mcp          (run from the project folder)
No third-party MCP SDK needed.
"""
from __future__ import annotations

import asyncio
import json
import sys

from . import analysis as an, integrations, pipeline, reports, store, traffic

PROTOCOL = "2024-11-05"

TOOLS = [
    {"name": "crawl_site", "description": "Crawl and audit a website (technical SEO, GEO, duplicates, hreflang, structured data, accessibility…). Saves the crawl and returns a summary with crawl_id.",
     "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}, "max_pages": {"type": "integer", "default": 100},
                                                      "js_render": {"type": "boolean", "default": False}, "crawl_sitemap": {"type": "boolean", "default": False},
                                                      "config": {"type": "object", "description": "Extra CrawlConfig fields (auth, include, exclude, custom_search, extractions, custom_js…)"}},
                     "required": ["url"]}},
    {"name": "list_crawls", "description": "List saved crawls (optionally for one site).",
     "inputSchema": {"type": "object", "properties": {"site": {"type": "string"}}}},
    {"name": "get_issues", "description": "Issues found in a crawl, most severe first, with example URLs.",
     "inputSchema": {"type": "object", "properties": {"crawl_id": {"type": "integer"}, "severity": {"type": "string", "enum": ["critical", "warning", "info"]},
                                                      "category": {"type": "string"}, "limit": {"type": "integer", "default": 30}}, "required": ["crawl_id"]}},
    {"name": "list_reports", "description": "Names of available tabular reports (internal, redirects, hreflang, duplicates_near, structured, …).",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "get_report", "description": "Rows from a report of a crawl, with optional text filter.",
     "inputSchema": {"type": "object", "properties": {"crawl_id": {"type": "integer"}, "report": {"type": "string"}, "query": {"type": "string"},
                                                      "limit": {"type": "integer", "default": 50}}, "required": ["crawl_id", "report"]}},
    {"name": "compare_crawls", "description": "Diff two crawls: new/removed URLs, changed titles/status/indexability, issue deltas.",
     "inputSchema": {"type": "object", "properties": {"old_id": {"type": "integer"}, "new_id": {"type": "integer"}}, "required": ["old_id", "new_id"]}},
    {"name": "generate_sitemap", "description": "Generate sitemap.xml from a crawl (indexable 200 pages). Writes to `path` if given, else returns the XML.",
     "inputSchema": {"type": "object", "properties": {"crawl_id": {"type": "integer"}, "path": {"type": "string"}}, "required": ["crawl_id"]}},
    {"name": "pagespeed", "description": "Run Google PageSpeed Insights (Lighthouse + field data) for a URL.",
     "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}, "strategy": {"type": "string", "enum": ["mobile", "desktop"]}}, "required": ["url"]}},
    {"name": "traffic_stats", "description": "First-party traffic analytics (views, visitors, top pages, referrers) for a pinned site.",
     "inputSchema": {"type": "object", "properties": {"site": {"type": "string"}, "days": {"type": "integer", "default": 30}}, "required": ["site"]}},
]


def _text(obj) -> dict:
    return {"content": [{"type": "text", "text": obj if isinstance(obj, str) else json.dumps(obj, indent=1, default=str)}]}


def call_tool(name: str, a: dict) -> dict:
    if name == "crawl_site":
        cfg = dict(a.get("config") or {})
        cfg.update(max_pages=a.get("max_pages", 100), js_render=a.get("js_render", False), crawl_sitemap=a.get("crawl_sitemap", False))
        result, site, issues = asyncio.run(pipeline.run_full(a["url"], cfg, label="mcp"))
        top = [{"severity": i.severity, "issue": i.message, "count": len(i.urls)} for i in sorted(issues, key=lambda i: ({"critical": 0, "warning": 1, "info": 2}[i.severity], -len(i.urls)))[:15]]
        return _text({"crawl_id": result["crawl_id"], "pages": result["pages_crawled"], "score": result["score"], "geo_score": result["geo"]["score"],
                      "indexability": result["indexability"], "status_codes": result["status_codes"], "top_issues": top})
    if name == "list_crawls":
        return _text(store.list_crawls(a.get("site")))
    if name == "get_issues":
        _, issues, _ = store.load_crawl(a["crawl_id"])
        order = {"critical": 0, "warning": 1, "info": 2}
        sel = [i for i in issues if (not a.get("severity") or i.severity == a["severity"]) and (not a.get("category") or i.category == a["category"])]
        sel.sort(key=lambda i: (order[i.severity], -len(i.urls)))
        return _text([i.to_dict() for i in sel[:a.get("limit", 30)]])
    if name == "list_reports":
        return _text(reports.catalog())
    if name == "get_report":
        site, issues, _ = store.load_crawl(a["crawl_id"])
        cols, rows = reports.issues_table(issues) if a["report"] == "issues" else reports.get(site, a["report"])
        q = (a.get("query") or "").lower()
        if q:
            rows = [r for r in rows if any(q in str(c).lower() for c in r)]
        return _text({"columns": cols, "total": len(rows), "rows": rows[:a.get("limit", 50)]})
    if name == "compare_crawls":
        d = store.compare_crawls(a["old_id"], a["new_id"])
        return _text({"summary": d["summary"], "added": d["added"][:40], "removed": d["removed"][:40], "changes": d["changes"][:60], "issue_changes": d["issue_changes"][:25]})
    if name == "generate_sitemap":
        files = an.generate_sitemaps(store.load_crawl(a["crawl_id"])[0])
        if a.get("path"):
            from pathlib import Path
            for n, x in files.items():
                p = Path(a["path"]) if len(files) == 1 else Path(a["path"]).with_name(n)
                p.write_text(x, encoding="utf-8")
            return _text(f"Wrote {len(files)} file(s) to {a['path']}")
        return _text(next(iter(files.values())))
    if name == "pagespeed":
        return _text(asyncio.run(integrations.psi(a["url"], a.get("strategy", "mobile"))))
    if name == "traffic_stats":
        return _text(traffic.stats(a["site"], a.get("days", 30)))
    raise ValueError(f"unknown tool {name}")


def handle(msg: dict) -> dict | None:
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:  # notification
        return None
    try:
        if method == "initialize":
            res = {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}}, "serverInfo": {"name": "seo-suite", "version": "1.0"}}
        elif method == "tools/list":
            res = {"tools": TOOLS}
        elif method == "tools/call":
            try:
                res = call_tool(params.get("name", ""), params.get("arguments") or {})
            except Exception as e:  # tool errors are reported in-band so the model can react
                res = {"content": [{"type": "text", "text": f"Error: {type(e).__name__}: {e}"}], "isError": True}
        elif method == "ping":
            res = {}
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": res}
    except Exception as e:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}}


def serve() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            reply = handle(json.loads(line))
        except json.JSONDecodeError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    serve()
