"""Command-line automation. Examples:

  python -m app.cli crawl https://example.com --max-pages 500 --export-dir out --formats csv,xlsx --fail-on critical
  python -m app.cli crawl https://example.com --config crawl.json --compare-last --email me@x.com
  python -m app.cli list | export 3 --dir out --formats xlsx | compare 2 3 | sitemap 3 --out sitemap.xml
  python -m app.cli schedule add https://example.com --every weekly --email me@x.com --exports xlsx
  python -m app.cli run-due        # put this in cron / Windows Task Scheduler (every 5-15 min)
  python -m app.cli serve --port 8080 | mcp
Exit code 2 when --fail-on is triggered (use it in CI to gate deploys).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

from . import analysis as an, notify, pipeline, sched, store


def _emit(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _config_from_args(a) -> dict:
    cfg = json.loads(Path(a.config).read_text(encoding="utf-8")) if a.config else {}
    for k in ("max_pages", "max_depth", "concurrency", "delay_ms"):
        if getattr(a, k, None) is not None:
            cfg[k] = getattr(a, k)
    if a.js:
        cfg["js_render"] = True
    if a.mobile:
        cfg["mobile"] = cfg["js_render"] = True
    if a.sitemap_seed:
        cfg["crawl_sitemap"] = True
    if a.ignore_robots:
        cfg["respect_robots"] = False
    if a.user_agent:
        cfg["user_agent"] = a.user_agent
    if a.basic_auth:
        u, _, pw = a.basic_auth.partition(":")
        cfg.setdefault("auth", {})["basic"] = {"user": u, "password": pw}
    for flag, key in ((a.include, "include"), (a.exclude, "exclude")):
        if flag:
            cfg[key] = flag
    return cfg


async def cmd_crawl(a) -> int:
    def prog(done, total, url):
        if not a.quiet:
            print(f"\r[{done}/{total}] {url[:90]:90}", end="", file=sys.stderr, flush=True)
    result, site, issues = await pipeline.run_full(a.url, _config_from_args(a), progress=prog, label=a.label, deep=not a.no_deep)
    cid = result["crawl_id"]
    if not a.quiet:
        print(file=sys.stderr)
    sev = {"critical": 0, "warning": 0, "info": 0}
    for i in issues:
        sev[i.severity] += len(i.urls)
    prev = store.previous_crawl_id(site.base, cid)
    diff = store.compare_crawls(prev, cid) if (prev and a.compare_last) else None
    out = {"crawl_id": cid, "site": site.base, "pages": result["pages_crawled"], "score": result["score"]["overall"],
           "geo_score": result["geo"]["score"], "issues": sev, "duration_s": result["duration_s"]}
    if diff:
        out["vs_previous"] = diff["summary"]
    if a.export_dir:
        out["files"] = pipeline.export_files(cid, a.export_dir, [f.strip() for f in a.formats.split(",") if f.strip()])
    if a.email:
        subject, body = notify.crawl_digest(site.base, result, diff)
        try:
            await asyncio.to_thread(notify.send_email, a.email, subject, body, out.get("files", []))
            out["emailed"] = a.email
        except Exception as e:
            out["email_error"] = str(e)
    _emit(out)
    fail = a.fail_on or ""
    if fail == "critical" and sev["critical"]:
        return 2
    if fail == "warning" and (sev["critical"] or sev["warning"]):
        return 2
    if a.min_score is not None and result["score"]["overall"] < a.min_score:
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="seo-suite", description="SEO Suite command line")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("crawl", help="crawl + audit a site and save the result")
    c.add_argument("url")
    c.add_argument("--max-pages", type=int, dest="max_pages")
    c.add_argument("--max-depth", type=int, dest="max_depth")
    c.add_argument("--concurrency", type=int)
    c.add_argument("--delay-ms", type=int, dest="delay_ms")
    c.add_argument("--config", help="JSON file with CrawlConfig fields (auth, custom_search, extractions, include/exclude…)")
    c.add_argument("--js", action="store_true", help="render with headless Chromium (needs playwright)")
    c.add_argument("--mobile", action="store_true", help="mobile rendering + mobile usability metrics")
    c.add_argument("--sitemap-seed", action="store_true", help="also crawl URLs from the sitemap (finds orphans)")
    c.add_argument("--ignore-robots", action="store_true")
    c.add_argument("--user-agent")
    c.add_argument("--basic-auth", metavar="USER:PASS")
    c.add_argument("--include", action="append", help="regex; repeatable")
    c.add_argument("--exclude", action="append", help="regex; repeatable")
    c.add_argument("--label", default="cli")
    c.add_argument("--no-deep", action="store_true", help="skip near-duplicate / spelling analysis (faster)")
    c.add_argument("--export-dir")
    c.add_argument("--formats", default="csv", help="csv,xlsx,json")
    c.add_argument("--compare-last", action="store_true")
    c.add_argument("--email")
    c.add_argument("--fail-on", choices=["critical", "warning"])
    c.add_argument("--min-score", type=int)
    c.add_argument("--quiet", action="store_true")

    sub.add_parser("list", help="list saved crawls").add_argument("--site")
    e = sub.add_parser("export", help="export a saved crawl")
    e.add_argument("crawl_id", type=int)
    e.add_argument("--dir", default="exports")
    e.add_argument("--formats", default="xlsx")
    cm = sub.add_parser("compare", help="compare two crawls")
    cm.add_argument("old", type=int)
    cm.add_argument("new", type=int)
    sm = sub.add_parser("sitemap", help="generate sitemap.xml from a crawl")
    sm.add_argument("crawl_id", type=int)
    sm.add_argument("--out", default="sitemap.xml")
    sm.add_argument("--images", action="store_true")
    ps = sub.add_parser("psi", help="PageSpeed Insights")
    ps.add_argument("url")
    ps.add_argument("--strategy", default="mobile")
    sc = sub.add_parser("schedule", help="manage scheduled crawls")
    ssub = sc.add_subparsers(dest="sub", required=True)
    sa = ssub.add_parser("add")
    sa.add_argument("url")
    sa.add_argument("--every", default="daily", help="hourly|daily|weekly|monthly|<minutes>")
    sa.add_argument("--hour", type=int, default=3)
    sa.add_argument("--max-pages", type=int, default=200)
    sa.add_argument("--email", default="")
    sa.add_argument("--exports", default="")
    sa.add_argument("--notify-on", default="changes", choices=["changes", "always"])
    ssub.add_parser("list")
    sd = ssub.add_parser("delete")
    sd.add_argument("id", type=int)
    sub.add_parser("run-due", help="run any schedules that are due, then exit")
    sv = sub.add_parser("serve", help="start the web UI")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    sub.add_parser("mcp", help="start the MCP server on stdio")

    a = ap.parse_args(argv)
    if a.cmd == "crawl":
        return asyncio.run(cmd_crawl(a))
    if a.cmd == "list":
        _emit(store.list_crawls(a.site))
    elif a.cmd == "export":
        _emit(pipeline.export_files(a.crawl_id, a.dir, [f.strip() for f in a.formats.split(",")]))
    elif a.cmd == "compare":
        d = store.compare_crawls(a.old, a.new)
        _emit({"summary": d["summary"], "added": d["added"][:50], "removed": d["removed"][:50], "issue_changes": d["issue_changes"][:30]})
    elif a.cmd == "sitemap":
        site = store.load_crawl(a.crawl_id)[0]
        files = an.generate_sitemaps(site, include_images=a.images)
        out = Path(a.out)
        for name, xml in files.items():
            target = out if len(files) == 1 else out.with_name(name)
            target.write_text(xml, encoding="utf-8")
            print("wrote", target)
    elif a.cmd == "psi":
        from . import integrations
        _emit(asyncio.run(integrations.psi(a.url, a.strategy)))
    elif a.cmd == "schedule":
        if a.sub == "add":
            _emit({"id": store.add_schedule(a.url, {"max_pages": a.max_pages}, a.every, a.hour, a.email, a.exports, a.notify_on)})
        elif a.sub == "list":
            _emit(store.list_schedules())
        else:
            store.delete_schedule(a.id)
    elif a.cmd == "run-due":
        _emit(asyncio.run(sched.run_due()))
    elif a.cmd == "serve":
        import uvicorn
        uvicorn.run("app.main:app", host=a.host, port=a.port)
    elif a.cmd == "mcp":
        from . import mcp_server
        mcp_server.serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
