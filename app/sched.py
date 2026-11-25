"""Scheduler: runs due crawls (in-process loop for the web app; `python -m app.cli run-due` for cron/Task Scheduler)."""
from __future__ import annotations

import asyncio
import json
import tempfile

from . import notify, pipeline, store


async def run_schedule(s: dict) -> dict:
    cfg = json.loads(s["config"] or "{}")
    result, site, issues = await pipeline.run_full(s["site"], cfg, label=f"scheduled #{s['id']}")
    cid = result["crawl_id"]
    prev = store.previous_crawl_id(site.base, cid)
    diff = store.compare_crawls(prev, cid) if prev else None
    status = {"crawl_id": cid, "emailed": False}
    store.mark_ran(s["id"], cid)
    if s.get("email"):
        changed = bool(diff and (diff["summary"]["issues_worse"] or diff["summary"]["added"] or diff["summary"]["removed"]))
        if s.get("notify_on") == "always" or changed or diff is None:
            subject, body = notify.crawl_digest(site.base, result, diff)
            files: list[str] = []
            formats = [f for f in (s.get("exports") or "").split(",") if f in ("csv", "xlsx", "json")]
            if formats:
                files = pipeline.export_files(cid, tempfile.mkdtemp(prefix="seo_export_"), formats)
            try:
                await asyncio.to_thread(notify.send_email, s["email"], subject, body, files)
                status["emailed"] = True
            except Exception as e:
                status["email_error"] = str(e)[:200]
    return status


async def run_due() -> list[dict]:
    out = []
    for s in store.due_schedules():
        try:
            out.append({"schedule": s["id"], **await run_schedule(s)})
        except Exception as e:
            store.mark_ran(s["id"], None)
            out.append({"schedule": s["id"], "error": f"{type(e).__name__}: {e}"})
    return out


async def loop(poll_seconds: int = 30) -> None:
    while True:
        try:
            await run_due()
        except Exception:
            pass
        await asyncio.sleep(poll_seconds)
