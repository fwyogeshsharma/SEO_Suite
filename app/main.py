"""FastAPI app: run `uvicorn app.main:app --reload`."""
from __future__ import annotations

import asyncio
import io
import json
import uuid
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import ai, analysis as an, integrations, notify, pipeline, reports, sched, store, tracking, traffic
from .crawler import normalize_base
from .keywords import content_gap, extract_keywords, suggest_keywords


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(sched.loop())
    yield
    task.cancel()


app = FastAPI(title="SEO Suite", lifespan=lifespan)
STATIC = Path(__file__).parent / "static"
JOBS: dict[str, dict] = {}
SITES: dict[str, object] = {}  # base -> SiteData (latest crawl, in memory)


def _crawl(crawl_id: int):
    c = store.load_crawl(crawl_id)
    if not c:
        raise HTTPException(404, "Crawl not found")
    return c


def _site_for(site: str):
    """Latest in-memory crawl for a site, else the latest saved one."""
    base = normalize_base(site)
    if base in SITES:
        return SITES[base]
    cid = store.latest_crawl_id(base)
    if cid:
        return store.load_crawl(cid)[0]
    return None


# ---------------------------------------------------------------- crawl / audit
class AuditReq(BaseModel):
    url: str
    max_pages: int = 50
    config: dict = {}
    label: str = ""
    deep: bool = True


async def _run_audit(job_id: str, req: AuditReq):
    job = JOBS[job_id]
    try:
        def prog(done, total, u):
            job.update(progress=done, total=total, current=u)
        cfg = dict(req.config or {})
        cfg["max_pages"] = req.max_pages if "max_pages" not in cfg else cfg["max_pages"]
        job.update(total=cfg["max_pages"])
        result, site, _ = await pipeline.run_full(req.url, cfg, progress=prog, label=req.label, deep=req.deep)
        SITES[site.base] = site
        job.update(status="done", result=result)
    except Exception as e:  # surface to UI
        job.update(status="error", error=f"{type(e).__name__}: {e}")


@app.post("/api/audit")
async def start_audit(req: AuditReq, bg: BackgroundTasks):
    jid = uuid.uuid4().hex[:10]
    JOBS[jid] = {"status": "running", "progress": 0, "total": req.max_pages, "current": ""}
    bg.add_task(_run_audit, jid, req)
    return {"job_id": jid}


@app.get("/api/audit/{job_id}")
async def audit_status(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(404, "unknown job")
    return JOBS[job_id]


@app.get("/api/history")
async def history(site: str):
    return tracking.audit_history(normalize_base(site))


@app.get("/api/crawls")
async def crawls(site: str | None = None):
    return store.list_crawls(normalize_base(site) if site else None)


@app.get("/api/crawls/{crawl_id}")
async def crawl_summary(crawl_id: int):
    site, issues, summary = _crawl(crawl_id)
    SITES[site.base] = site
    summary = dict(summary)
    summary["issues"] = [i.to_dict() for i in issues]
    summary["crawl_id"] = crawl_id
    return summary


@app.delete("/api/crawls/{crawl_id}")
async def crawl_delete(crawl_id: int):
    store.delete_crawl(crawl_id)
    return {"ok": True}


# ---------------------------------------------------------------- reports (server-side tables)
@app.get("/api/reports")
async def report_catalog():
    return reports.catalog()


def _table(crawl_id: int, name: str, params: dict):
    site, issues, _ = _crawl(crawl_id)
    if name == "issues":
        return reports.issues_table(issues)
    try:
        return reports.get(site, name, **params)
    except KeyError:
        raise HTTPException(404, "unknown report")


@app.get("/api/crawl/{crawl_id}/report/{name}")
async def crawl_report(crawl_id: int, name: str, q: str = "", sort: int = -1, desc: bool = False, offset: int = 0, limit: int = 200,
                       threshold: float | None = None):
    params = {"threshold": threshold} if threshold is not None else {}
    cols, rows = await asyncio.to_thread(_table, crawl_id, name, params)
    if q:
        ql = q.lower()
        rows = [r for r in rows if any(ql in str(c).lower() for c in r)]
    if 0 <= sort < len(cols):
        rows = sorted(rows, key=lambda r: (isinstance(r[sort], str), r[sort] if r[sort] != "" else -1), reverse=desc)
    return {"columns": cols, "rows": rows[offset:offset + min(limit, 1000)], "total": len(rows)}


@app.get("/api/crawl/{crawl_id}/export/{name}.csv")
async def export_csv(crawl_id: int, name: str):
    cols, rows = await asyncio.to_thread(_table, crawl_id, name, {})
    return Response(reports.to_csv(cols, rows), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="crawl{crawl_id}_{name}.csv"'})


@app.get("/api/crawl/{crawl_id}/export.xlsx")
async def export_xlsx(crawl_id: int):
    site, issues, _ = _crawl(crawl_id)
    data = await asyncio.to_thread(reports.full_workbook, site, issues)
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="crawl{crawl_id}.xlsx"'})


@app.get("/api/crawl/{crawl_id}/export.json")
async def export_json(crawl_id: int):
    site, issues, summary = _crawl(crawl_id)
    return {"summary": summary, "issues": [i.to_dict(full=True) for i in issues]}


@app.get("/api/feed/{name}.csv")
async def feed_csv(name: str, site: str):
    """Always-latest CSV for a site — use in Google Sheets (=IMPORTDATA) as a Looker/Data Studio source."""
    cid = store.latest_crawl_id(normalize_base(site))
    if not cid:
        raise HTTPException(404, "No saved crawl for this site")
    return await export_csv(cid, name)


@app.get("/api/crawl/{crawl_id}/report.html", response_class=HTMLResponse)
async def html_report(crawl_id: int):
    site, issues, s = _crawl(crawl_id)
    esc = lambda x: str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    rows = "".join(f"<tr><td class='{i.severity}'>{i.severity}</td><td>{esc(i.category)}</td><td>{esc(i.message)}</td><td>{len(i.urls)}</td><td>{esc(i.fix_hint)}</td></tr>"
                   for i in sorted(issues, key=lambda i: ({'critical': 0, 'warning': 1, 'info': 2}[i.severity], -len(i.urls))))
    cats = "".join(f"<li>{esc(k)}: <b>{v}</b></li>" for k, v in s["score"]["by_category"].items())
    return f"""<!doctype html><meta charset=utf-8><title>SEO report {esc(site.base)}</title>
<style>body{{font:14px system-ui;max-width:1000px;margin:30px auto;padding:0 16px}}table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #ddd;padding:6px;text-align:left;vertical-align:top}}
.critical{{color:#c0392b}}.warning{{color:#d68910}}.info{{color:#2e86c1}}.big{{font-size:44px;font-weight:700}}</style>
<h1>SEO audit report — {esc(site.base)}</h1><p>{s['pages_crawled']} pages crawled</p>
<div class=big>{s['score']['overall']}<small style='font-size:16px'> technical</small> &nbsp; {s.get('geo', {}).get('score', '-')}<small style='font-size:16px'> GEO</small></div>
<h3>Score by category</h3><ul>{cats}</ul><h3>Indexability</h3><pre>{esc(json.dumps(s.get('indexability', {})))}</pre>
<h3>Status codes</h3><pre>{esc(json.dumps(s.get('status_codes', {})))}</pre>
<h3>Issues</h3><table><tr><th>Severity</th><th>Category</th><th>Issue</th><th>URLs</th><th>Fix</th></tr>{rows}</table>"""


# ---------------------------------------------------------------- visualisations, sitemap, segments, compare
@app.get("/api/crawl/{crawl_id}/graph")
async def crawl_graph(crawl_id: int, limit: int = 250):
    site = _crawl(crawl_id)[0]
    return {"graph": await asyncio.to_thread(an.graph_data, site, limit), "tree": an.directory_tree(site), "depth": an.depth_histogram(site)}


@app.get("/api/crawl/{crawl_id}/sitemap")
async def crawl_sitemap(crawl_id: int, images: bool = False, hreflang: bool = True, priority: bool = True, changefreq: str = "",
                        include_noindex: bool = False, preview: bool = False):
    site = _crawl(crawl_id)[0]
    files = an.generate_sitemaps(site, images, hreflang, priority, changefreq, include_noindex)
    if len(files) == 1:
        xml = next(iter(files.values()))
        return Response(xml, media_type="application/xml" if not preview else "text/plain; charset=utf-8",
                        headers={} if preview else {"Content-Disposition": 'attachment; filename="sitemap.xml"'})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n, x in files.items():
            z.writestr(n, x)
    return Response(buf.getvalue(), media_type="application/zip", headers={"Content-Disposition": 'attachment; filename="sitemaps.zip"'})


class SegmentReq(BaseModel):
    site: str
    name: str
    match: str = "all"
    rules: list[dict]


@app.post("/api/segments")
async def segment_save(req: SegmentReq):
    store.save_segment(normalize_base(req.site), {"name": req.name, "match": req.match, "rules": req.rules})
    return {"ok": True}


@app.delete("/api/segments")
async def segment_delete(site: str, name: str):
    store.delete_segment(normalize_base(site), name)
    return {"ok": True}


@app.get("/api/crawl/{crawl_id}/segments")
async def crawl_segments(crawl_id: int):
    site, issues, _ = _crawl(crawl_id)
    saved = store.get_segments(site.base)
    segs = saved or an.auto_segments(site)
    rep = await asyncio.to_thread(an.segment_report, site, segs, {i.id: i.urls for i in issues})
    return {"segments": rep, "custom": bool(saved), "fields": ["url", "path", "folder", "status", "indexability", "indexability_reason", "depth",
                                                           "words", "load_ms", "title", "h1", "lang", "schema", "inlinks", "size", "in_sitemap",
                                                           "canonical", "content_type", "meta_description", "link_score", "issue:<issue id>"]}


@app.get("/api/compare")
async def compare(old: int, new: int):
    try:
        return await asyncio.to_thread(store.compare_crawls, old, new)
    except KeyError:
        raise HTTPException(404, "Crawl not found")


# ---------------------------------------------------------------- scheduling & email
class ScheduleReq(BaseModel):
    site: str
    config: dict = {}
    every: str = "daily"
    hour: int = 3
    email: str = ""
    exports: str = ""
    notify_on: str = "changes"


@app.get("/api/schedules")
async def schedules():
    return store.list_schedules()


@app.post("/api/schedules")
async def schedule_add(req: ScheduleReq):
    try:
        return {"id": store.add_schedule(req.site, req.config, req.every, req.hour, req.email, req.exports, req.notify_on)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/schedules/{sid}")
async def schedule_del(sid: int):
    store.delete_schedule(sid)
    return {"ok": True}


@app.post("/api/schedules/{sid}/toggle")
async def schedule_toggle(sid: int, enabled: bool):
    store.set_schedule_enabled(sid, enabled)
    return {"ok": True}


@app.post("/api/schedules/{sid}/run")
async def schedule_run(sid: int, bg: BackgroundTasks):
    s = next((x for x in store.list_schedules() if x["id"] == sid), None)
    if not s:
        raise HTTPException(404, "unknown schedule")
    bg.add_task(sched.run_schedule, s)
    return {"started": True}


class EmailReq(BaseModel):
    to: str
    formats: list[str] = []


@app.post("/api/crawl/{crawl_id}/email")
async def crawl_email(crawl_id: int, req: EmailReq):
    site, issues, summary = _crawl(crawl_id)
    prev = store.previous_crawl_id(site.base, crawl_id)
    diff = store.compare_crawls(prev, crawl_id) if prev else None
    subject, body = notify.crawl_digest(site.base, {**summary, "issues": [i.to_dict() for i in issues]}, diff)
    import tempfile
    files = pipeline.export_files(crawl_id, tempfile.mkdtemp(prefix="seo_export_"), req.formats) if req.formats else []
    try:
        await asyncio.to_thread(notify.send_email, req.to, subject, body, files)
    except Exception as e:
        raise HTTPException(501, str(e))
    return {"sent": True}


# ---------------------------------------------------------------- integrations
@app.get("/api/settings")
async def settings_get():
    return integrations.settings_view()


@app.post("/api/settings")
async def settings_set(values: dict):
    return {"updated": integrations.set_env({k: str(v) for k, v in values.items()})}


@app.get("/api/ga")
async def ga(days: int = 28):
    try:
        return await integrations.ga_pages(days)
    except (RuntimeError, Exception) as e:
        raise HTTPException(501, str(e))


@app.get("/api/gsc/pages")
async def gsc_pages(site: str, days: int = 28):
    try:
        return await integrations.gsc_pages(site, days)
    except Exception as e:
        raise HTTPException(501, str(e))


class InspectReq(BaseModel):
    site: str
    urls: list[str]


@app.post("/api/gsc/inspect")
async def gsc_inspect(req: InspectReq):
    try:
        await integrations.google_token("gsc")
    except Exception as e:
        raise HTTPException(501, str(e))
    return await integrations.gsc_inspect_many(req.site, req.urls[:50])


@app.get("/api/psi")
async def psi(url: str, strategy: str = "mobile"):
    try:
        return await integrations.psi(url, strategy)
    except Exception as e:
        raise HTTPException(502, str(e))


class PsiBatch(BaseModel):
    crawl_id: int
    limit: int = 10
    strategy: str = "mobile"


@app.post("/api/psi/batch")
async def psi_batch(req: PsiBatch):
    site = _crawl(req.crawl_id)[0]
    scores = an.link_score(site)
    urls = [p.url for p in sorted(an.html_pages(site), key=lambda p: -scores.get(p.url, 0))][:min(req.limit, 50)]
    return await integrations.psi_many(urls, req.strategy)


@app.get("/api/crawl/{crawl_id}/enrich")
async def crawl_enrich(crawl_id: int, days: int = 28, sources: str = "traffic,gsc,ga"):
    site = _crawl(crawl_id)[0]
    return await integrations.enrich(site, days, tuple(s for s in sources.split(",") if s))


# ---------------------------------------------------------------- AI
@app.get("/api/ai/info")
async def ai_info():
    return ai.info()


class AiBatch(BaseModel):
    crawl_id: int
    prompt: str
    urls: list[str] = []
    limit: int = 10


@app.post("/api/ai/batch")
async def ai_batch(req: AiBatch):
    """Run a custom prompt over pages. Placeholders: {url} {title} {h1} {description} {text} {words}."""
    if not ai.ai_available():
        raise HTTPException(501, "No AI provider configured (Settings tab)")
    site = _crawl(req.crawl_id)[0]
    pages = [site.pages[u] for u in req.urls if u in site.pages] or an.html_pages(site)
    pages = pages[:min(req.limit, 50)]
    sem = asyncio.Semaphore(3)

    async def one(p):
        async with sem:
            try:
                out = await asyncio.to_thread(ai.run_prompt, req.prompt, {"url": p.url, "title": p.title, "h1": " / ".join(p.h1),
                                                                         "description": p.meta_description, "text": p.text[:3000], "words": p.word_count})
            except Exception as e:
                out = f"ERROR: {str(e)[:200]}"
            return {"url": p.url, "result": out}
    return await asyncio.gather(*(one(p) for p in pages))


@app.post("/api/crawl/{crawl_id}/semantic-ai")
async def semantic_ai(crawl_id: int, threshold: float = 0.85, limit: int = 400):
    """Semantic similarity + clusters from real embeddings (OpenAI / Gemini / Ollama)."""
    site = _crawl(crawl_id)[0]
    pages = [p for p in an.html_pages(site) if p.word_count >= 30][:limit]
    vecs = await asyncio.to_thread(ai.embed, [f"{p.title}\n{p.text[:4000]}" for p in pages]) if ai.ai_available() else None
    if vecs is None:
        raise HTTPException(501, "Embeddings need OpenAI, Gemini or Ollama as the AI provider (Anthropic has no embeddings API). TF-IDF similarity is always available.")
    v = {p.url: e for p, e in zip(pages, vecs)}
    return {"pairs": an.semantic_pairs(site, threshold, v)[:300], "clusters": an.clusters(site, 0.8, v)[:100]}


@app.get("/api/crawl/{crawl_id}/languagetool")
async def languagetool(crawl_id: int, url: str):
    import os
    lt = os.getenv("LANGUAGETOOL_URL")
    if not lt:
        raise HTTPException(501, "Set LANGUAGETOOL_URL (e.g. http://localhost:8081) for full grammar checking")
    p = _crawl(crawl_id)[0].pages.get(url)
    if not p:
        raise HTTPException(404, "URL not in crawl")
    return await an.languagetool_check(p.text, lt, "auto")


class FixReq(BaseModel):
    site: str
    url: str
    kind: str  # meta | faq | alt


@app.post("/api/fix")
async def fix(req: FixReq):
    s = _site_for(req.site)
    page = s.pages.get(req.url) if s else None
    if not page:
        raise HTTPException(404, "Page not in latest crawl — run an audit first")
    kws = [k["keyword"] for k in extract_keywords(s, 8)]
    if req.kind == "meta":
        return await asyncio.to_thread(ai.fix_page_meta, page.url, page.title, page.meta_description, page.h1, page.text, kws)
    if req.kind == "faq":
        return await asyncio.to_thread(ai.faq_schema, page.url, page.text)
    if req.kind == "alt":
        return await asyncio.to_thread(ai.alt_text_suggestions, page.url, page.text)
    raise HTTPException(400, "kind must be meta|faq|alt")


class PlanReq(BaseModel):
    site: str
    issues: list[dict]


@app.post("/api/plan")
async def plan(req: PlanReq):
    return await asyncio.to_thread(ai.prioritize, req.issues, req.site)


class KwReq(BaseModel):
    seed: str
    site: str | None = None


@app.post("/api/keywords/ideas")
async def kw_ideas(req: KwReq):
    ideas = await suggest_keywords(req.seed)
    gaps = []
    s = _site_for(req.site) if req.site else None
    if s:
        gaps = content_gap(extract_keywords(s, 60), ideas)
    return {"ideas": ideas, "content_gaps": gaps}


class BriefReq(BaseModel):
    keyword: str


@app.post("/api/brief")
async def brief(req: BriefReq):
    return await asyncio.to_thread(ai.content_brief, req.keyword)


@app.get("/api/llms-txt", response_class=PlainTextResponse)
async def gen_llms(site: str, name: str = ""):
    s = _site_for(site)
    if not s:
        raise HTTPException(404, "Run an audit first")
    pages = [{"title": p.title, "url": p.url, "description": p.meta_description}
             for p in s.pages.values() if p.status == 200 and p.is_html]
    return ai.llms_txt(name or urlparse(s.base).netloc, s.base, pages)


# ---------------------------------------------------------------- rank / backlinks
class TrackReq(BaseModel):
    site: str
    keywords: list[str]


@app.post("/api/rank/track")
async def rank_track(req: TrackReq):
    tracking.track_keywords(urlparse(normalize_base(req.site)).netloc, req.keywords)
    return {"tracked": tracking.tracked(urlparse(normalize_base(req.site)).netloc)}


@app.post("/api/rank/check")
async def rank_check(site: str):
    host = urlparse(normalize_base(site)).netloc
    kws = tracking.tracked(host)
    if not kws:
        raise HTTPException(400, "No tracked keywords")
    try:
        return await tracking.check_ranks(host, kws)
    except RuntimeError as e:
        raise HTTPException(501, str(e))


@app.get("/api/rank/history")
async def rank_hist(site: str):
    return tracking.rank_history(urlparse(normalize_base(site)).netloc)


@app.get("/api/backlinks")
async def backlinks(site: str):
    try:
        return await tracking.backlink_summary(urlparse(normalize_base(site)).netloc)
    except RuntimeError as e:
        raise HTTPException(501, str(e))


@app.get("/api/gsc")
async def gsc(site: str, days: int = 28):
    try:
        return await tracking.gsc_queries(site, days)
    except Exception as e:
        raise HTTPException(501, str(e))


# ---------------------------------------------------------------- first-party traffic
class PinReq(BaseModel):
    site: str


@app.post("/api/traffic/pin")
async def traffic_pin(req: PinReq):
    try:
        host = await asyncio.to_thread(traffic.pin, req.site)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"host": host}


@app.delete("/api/traffic/pin")
async def traffic_unpin(site: str, delete_data: bool = False):
    await asyncio.to_thread(traffic.unpin, site, delete_data)
    return {"ok": True}


@app.get("/api/traffic/sites")
async def traffic_sites():
    return await asyncio.to_thread(traffic.pinned)


@app.get("/api/traffic/stats")
async def traffic_stats(site: str, days: int = 30):
    return await asyncio.to_thread(traffic.stats, site, days)


@app.get("/t.js")
async def tracker_script(request: Request):
    collect = str(request.base_url).rstrip("/") + "/t/collect"
    return Response(traffic.tracker_js(collect), media_type="application/javascript",
                    headers={"Cache-Control": "public, max-age=3600", "Access-Control-Allow-Origin": "*"})


@app.post("/t/collect")
async def tracker_collect(request: Request):
    """Public beacon endpoint; always 204 so the tracked site never sees errors."""
    cors = {"Access-Control-Allow-Origin": "*"}
    try:
        d = json.loads((await request.body())[:4096] or b"{}")
        fwd = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        ip = fwd or (request.client.host if request.client else "")
        await asyncio.to_thread(traffic.record_hit, str(d.get("s", "")), str(d.get("p", "/")),
                                str(d.get("r", "")), ip, request.headers.get("user-agent", ""))
    except Exception:
        pass
    return Response(status_code=204, headers=cors)


@app.get("/api/status")
async def status():
    return {"ai": ai.ai_available(), "ai_info": ai.info(), "dataforseo": tracking.dataforseo_configured(), "gsc": integrations.gsc_configured(),
            "ga": integrations.ga_configured(), "smtp": notify.smtp_configured()}


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
