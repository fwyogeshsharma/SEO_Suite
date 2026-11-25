"""Persistence: save/open crawls, crawl comparison, segments, schedules, cached PSI results."""
from __future__ import annotations

import json
import time
import zlib
from collections import OrderedDict
from dataclasses import fields

from . import analysis as an
from .audit import Issue
from .crawler import Page, SiteData
from .tracking import db

_PAGE_FIELDS = {f.name for f in fields(Page)}
_CACHE: "OrderedDict[int, tuple]" = OrderedDict()


def _init(con) -> None:
    con.executescript("""
    CREATE TABLE IF NOT EXISTS crawls(id INTEGER PRIMARY KEY, site TEXT, created REAL, label TEXT, pages INTEGER,
        score INTEGER, geo_score INTEGER, summary TEXT, data BLOB, issues BLOB);
    CREATE INDEX IF NOT EXISTS idx_crawls_site ON crawls(site, created);
    CREATE TABLE IF NOT EXISTS segments(site TEXT, name TEXT, spec TEXT, PRIMARY KEY(site, name));
    CREATE TABLE IF NOT EXISTS schedules(id INTEGER PRIMARY KEY, site TEXT, config TEXT, every TEXT, hour INTEGER,
        email TEXT, enabled INTEGER DEFAULT 1, last_run REAL, next_run REAL, last_crawl INTEGER, exports TEXT, notify_on TEXT);
    CREATE TABLE IF NOT EXISTS psi(url TEXT, strategy TEXT, created REAL, data TEXT, PRIMARY KEY(url, strategy));
    """)


def conn():
    con = db()
    _init(con)
    return con


# ------------------------------------------------------------------ save / open crawls
def _pack(obj) -> bytes:
    return zlib.compress(json.dumps(obj, default=str).encode(), 6)


def _unpack(b: bytes):
    return json.loads(zlib.decompress(b))


def save_crawl(site: SiteData, issues: list[Issue], summary: dict, label: str = "") -> int:
    data = {"base": site.base, "robots_txt": site.robots_txt, "sitemap_urls": site.sitemap_urls, "sitemap_found": site.sitemap_found,
            "sitemaps": site.sitemaps, "sitemap_meta": site.sitemap_meta, "llms_txt": site.llms_txt,
            "resources": site.resources, "external_status": site.external_status, "config": site.config,
            "started": site.started, "finished": site.finished, "pages": [p.__dict__ for p in site.pages.values()]}
    slim = {k: v for k, v in summary.items() if k not in ("issues",)}
    with conn() as con:
        cur = con.execute(
            "INSERT INTO crawls(site,created,label,pages,score,geo_score,summary,data,issues) VALUES(?,?,?,?,?,?,?,?,?)",
            (site.base, time.time(), label, len(site.pages), summary.get("score", {}).get("overall"),
             summary.get("geo", {}).get("score"), json.dumps(slim, default=str), _pack(data),
             _pack([i.to_dict(full=True) for i in issues])))
        return cur.lastrowid


def list_crawls(site: str | None = None, limit: int = 100) -> list[dict]:
    q = "SELECT id,site,created,label,pages,score,geo_score FROM crawls"
    args: tuple = ()
    if site:
        q += " WHERE site=?"
        args = (site,)
    with conn() as con:
        return [dict(r) for r in con.execute(q + " ORDER BY created DESC LIMIT ?", args + (limit,))]


def load_crawl(crawl_id: int) -> tuple[SiteData, list[Issue], dict] | None:
    if crawl_id in _CACHE:
        _CACHE.move_to_end(crawl_id)
        return _CACHE[crawl_id]
    with conn() as con:
        r = con.execute("SELECT summary,data,issues FROM crawls WHERE id=?", (crawl_id,)).fetchone()
    if not r:
        return None
    d = _unpack(r["data"])
    pages = {}
    for pd in d["pages"]:
        p = Page(**{k: v for k, v in pd.items() if k in _PAGE_FIELDS})
        pages[p.url] = p
    site = SiteData(base=d["base"], pages=pages, robots_txt=d["robots_txt"], sitemap_urls=d["sitemap_urls"],
                    sitemap_found=d["sitemap_found"], sitemaps=d.get("sitemaps", []), sitemap_meta=d.get("sitemap_meta", {}),
                    llms_txt=d["llms_txt"], resources=d.get("resources", {}), external_status=d.get("external_status", {}),
                    config=d.get("config", {}), started=d.get("started", 0), finished=d.get("finished", 0))
    from .crawler import finalize
    finalize(site)
    issues = [Issue(i["id"], i["severity"], i["category"], i["message"], i["urls"], i.get("fix_hint", "")) for i in _unpack(r["issues"])]
    out = (site, issues, json.loads(r["summary"]))
    _CACHE[crawl_id] = out
    while len(_CACHE) > 4:
        _CACHE.popitem(last=False)
    return out


def delete_crawl(crawl_id: int) -> None:
    _CACHE.pop(crawl_id, None)
    with conn() as con:
        con.execute("DELETE FROM crawls WHERE id=?", (crawl_id,))


def latest_crawl_id(site: str) -> int | None:
    with conn() as con:
        r = con.execute("SELECT id FROM crawls WHERE site=? ORDER BY created DESC LIMIT 1", (site,)).fetchone()
    return r["id"] if r else None


def previous_crawl_id(site: str, before_id: int) -> int | None:
    with conn() as con:
        r = con.execute("SELECT id FROM crawls WHERE site=? AND id<? ORDER BY id DESC LIMIT 1", (site, before_id)).fetchone()
    return r["id"] if r else None


# ------------------------------------------------------------------ comparison
def compare_crawls(old_id: int, new_id: int) -> dict:
    a, b = load_crawl(old_id), load_crawl(new_id)
    if not a or not b:
        raise KeyError("crawl not found")
    (sa, ia, _), (sb, ib, _) = a, b
    ua, ub = set(sa.pages), set(sb.pages)
    changes: list[dict] = []

    def cmp(field_: str, get, only_html=True):
        for u in ua & ub:
            pa, pb = sa.pages[u], sb.pages[u]
            if only_html and not (pa.is_html and pb.is_html):
                continue
            x, y = get(sa, pa), get(sb, pb)
            if x != y:
                changes.append({"url": u, "field": field_, "old": x, "new": y})

    cmp("status", lambda s, p: p.status, False)
    cmp("indexability", lambda s, p: " ".join(an.indexability(s, p)).strip(), False)
    cmp("title", lambda s, p: p.title)
    cmp("meta_description", lambda s, p: p.meta_description)
    cmp("h1", lambda s, p: p.h1[0] if p.h1 else "")
    cmp("canonical", lambda s, p: p.canonical)
    cmp("robots", lambda s, p: p.robots_meta)
    cmp("depth", lambda s, p: p.depth, False)
    cmp("redirect_to", lambda s, p: p.redirect_final, False)
    cmp("hreflang", lambda s, p: len(p.hreflang))
    cmp("schema_types", lambda s, p: ",".join(sorted(set(p.schema_types))))
    for u in ua & ub:
        pa, pb = sa.pages[u], sb.pages[u]
        if pa.is_html and pb.is_html and pa.word_count and abs(pb.word_count - pa.word_count) / pa.word_count > 0.2:
            changes.append({"url": u, "field": "word_count", "old": pa.word_count, "new": pb.word_count})
        if pa.load_ms and abs(pb.load_ms - pa.load_ms) > max(300, pa.load_ms * 0.5):
            changes.append({"url": u, "field": "load_ms", "old": pa.load_ms, "new": pb.load_ms})
    ma, mb = {i.id: i for i in ia}, {i.id: i for i in ib}
    issue_changes = []
    for iid in sorted(set(ma) | set(mb)):
        ca, cb = len(ma[iid].urls) if iid in ma else 0, len(mb[iid].urls) if iid in mb else 0
        if ca != cb:
            ref = mb.get(iid) or ma[iid]
            old_urls, new_urls = set(ma[iid].urls) if iid in ma else set(), set(mb[iid].urls) if iid in mb else set()
            issue_changes.append({"id": iid, "severity": ref.severity, "message": ref.message, "old": ca, "new": cb,
                                  "delta": cb - ca, "newly_affected": sorted(new_urls - old_urls)[:25], "fixed": sorted(old_urls - new_urls)[:25]})
    issue_changes.sort(key=lambda c: (-abs(c["delta"])))
    meta = {r["id"]: r for r in list_crawls(limit=1000)}
    return {"old": meta.get(old_id), "new": meta.get(new_id), "added": sorted(ub - ua), "removed": sorted(ua - ub),
            "changes": changes, "issue_changes": issue_changes,
            "summary": {"added": len(ub - ua), "removed": len(ua - ub), "changed_urls": len({c['url'] for c in changes}),
                        "issues_worse": sum(1 for c in issue_changes if c["delta"] > 0), "issues_better": sum(1 for c in issue_changes if c["delta"] < 0)}}


# ------------------------------------------------------------------ segments
def save_segment(site: str, seg: dict) -> None:
    with conn() as con:
        con.execute("INSERT OR REPLACE INTO segments(site,name,spec) VALUES(?,?,?)", (site, seg["name"], json.dumps(seg)))


def delete_segment(site: str, name: str) -> None:
    with conn() as con:
        con.execute("DELETE FROM segments WHERE site=? AND name=?", (site, name))


def get_segments(site: str) -> list[dict]:
    with conn() as con:
        return [json.loads(r["spec"]) for r in con.execute("SELECT spec FROM segments WHERE site=? ORDER BY name", (site,))]


# ------------------------------------------------------------------ schedules
INTERVALS = {"hourly": 3600, "daily": 86400, "weekly": 7 * 86400, "monthly": 30 * 86400}


def _next_run(every: str, hour: int, after: float) -> float:
    import datetime as dt
    step = INTERVALS.get(every) or int(every) * 60
    if step >= 86400:  # align to the chosen hour of day
        d = dt.datetime.fromtimestamp(after)
        nxt = d.replace(hour=hour or 0, minute=0, second=0, microsecond=0)
        while nxt.timestamp() <= after:
            nxt += dt.timedelta(seconds=step)
        return nxt.timestamp()
    return after + step


def add_schedule(site: str, config: dict, every: str, hour: int = 3, email: str = "", exports: str = "", notify_on: str = "changes") -> int:
    if every not in INTERVALS and not str(every).isdigit():
        raise ValueError("every must be hourly|daily|weekly|monthly or a number of minutes")
    with conn() as con:
        cur = con.execute("INSERT INTO schedules(site,config,every,hour,email,enabled,next_run,exports,notify_on) VALUES(?,?,?,?,?,1,?,?,?)",
                          (site, json.dumps(config), every, hour, email, _next_run(every, hour, time.time()), exports, notify_on))
        return cur.lastrowid


def list_schedules() -> list[dict]:
    with conn() as con:
        return [dict(r) for r in con.execute("SELECT * FROM schedules ORDER BY id")]


def delete_schedule(sid: int) -> None:
    with conn() as con:
        con.execute("DELETE FROM schedules WHERE id=?", (sid,))


def set_schedule_enabled(sid: int, enabled: bool) -> None:
    with conn() as con:
        con.execute("UPDATE schedules SET enabled=? WHERE id=?", (int(enabled), sid))


def due_schedules(now: float | None = None) -> list[dict]:
    with conn() as con:
        return [dict(r) for r in con.execute("SELECT * FROM schedules WHERE enabled=1 AND next_run<=?", (now or time.time(),))]


def mark_ran(sid: int, crawl_id: int | None) -> None:
    with conn() as con:
        r = con.execute("SELECT every,hour FROM schedules WHERE id=?", (sid,)).fetchone()
        if r:
            now = time.time()
            con.execute("UPDATE schedules SET last_run=?, next_run=?, last_crawl=? WHERE id=?",
                        (now, _next_run(r["every"], r["hour"], now), crawl_id, sid))


# ------------------------------------------------------------------ PSI cache
def save_psi(url: str, strategy: str, data: dict) -> None:
    with conn() as con:
        con.execute("INSERT OR REPLACE INTO psi(url,strategy,created,data) VALUES(?,?,?,?)", (url, strategy, time.time(), json.dumps(data)))


def get_psi(url: str, strategy: str) -> dict | None:
    with conn() as con:
        r = con.execute("SELECT created,data FROM psi WHERE url=? AND strategy=?", (url, strategy)).fetchone()
    return {**json.loads(r["data"]), "fetched": r["created"]} if r else None
