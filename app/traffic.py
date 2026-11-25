"""First-party traffic analytics: pin a site, embed /t.js on it, view visits in the dashboard.

Privacy-friendly by design: no cookies, no raw IPs stored. A visitor is a daily-rotating hash of IP + user agent.
"""
from __future__ import annotations

import hashlib
import secrets
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse

from .tracking import db

BOT_HINTS = ("bot", "crawl", "spider", "slurp", "headless", "lighthouse", "preview")


def _init(con) -> None:
    con.executescript("""
    CREATE TABLE IF NOT EXISTS traffic_sites(host TEXT PRIMARY KEY, created REAL);
    CREATE TABLE IF NOT EXISTS traffic_hits(id INTEGER PRIMARY KEY, host TEXT, ts REAL, day TEXT, path TEXT,
                                            ref_host TEXT, visitor TEXT, device TEXT);
    CREATE INDEX IF NOT EXISTS idx_hits_host_ts ON traffic_hits(host, ts);
    CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
    """)


def _conn():
    con = db()
    _init(con)
    return con


def _salt(con) -> str:
    r = con.execute("SELECT v FROM meta WHERE k='traffic_salt'").fetchone()
    if r:
        return r["v"]
    s = secrets.token_hex(16)
    con.execute("INSERT INTO meta(k,v) VALUES('traffic_salt',?)", (s,))
    con.commit()
    return s


def host_of(site: str) -> str:
    site = site.strip()
    if "//" not in site:
        site = "https://" + site
    h = (urlparse(site).netloc or "").lower()
    return h[4:] if h.startswith("www.") else h


# ---- pinned sites ----
def pin(site: str) -> str:
    host = host_of(site)
    if not host:
        raise ValueError("invalid site")
    with _conn() as con:
        con.execute("INSERT OR IGNORE INTO traffic_sites(host,created) VALUES(?,?)", (host, time.time()))
    return host


def unpin(site: str, delete_data: bool = False) -> None:
    host = host_of(site)
    with _conn() as con:
        con.execute("DELETE FROM traffic_sites WHERE host=?", (host,))
        if delete_data:
            con.execute("DELETE FROM traffic_hits WHERE host=?", (host,))


def pinned() -> list[dict]:
    with _conn() as con:
        rows = con.execute("""SELECT s.host, s.created, COUNT(h.id) AS hits, MAX(h.ts) AS last_hit
                              FROM traffic_sites s LEFT JOIN traffic_hits h ON h.host=s.host
                              GROUP BY s.host ORDER BY s.created""").fetchall()
    return [dict(r) for r in rows]


def is_pinned(host: str) -> bool:
    with _conn() as con:
        return con.execute("SELECT 1 FROM traffic_sites WHERE host=?", (host,)).fetchone() is not None


# ---- collection ----
def _device(ua: str) -> str:
    u = ua.lower()
    if any(b in u for b in BOT_HINTS):
        return "bot"
    if "ipad" in u or "tablet" in u:
        return "tablet"
    if "mobi" in u or "android" in u or "iphone" in u:
        return "mobile"
    return "desktop"


def record_hit(site: str, path: str, referrer: str, ip: str, ua: str) -> bool:
    """Returns False if the site isn't pinned or the hit is a bot (ignored)."""
    host = host_of(site)
    device = _device(ua)
    if device == "bot":
        return False
    with _conn() as con:
        if not con.execute("SELECT 1 FROM traffic_sites WHERE host=?", (host,)).fetchone():
            return False
        now = datetime.now(timezone.utc)
        day = now.date().isoformat()
        visitor = hashlib.sha256(f"{_salt(con)}|{day}|{ip}|{ua}".encode()).hexdigest()[:16]
        ref_host = host_of(referrer) if referrer else ""
        if ref_host == host:
            ref_host = ""  # internal navigation isn't a referral
        path = (path or "/")[:300]
        con.execute("INSERT INTO traffic_hits(host,ts,day,path,ref_host,visitor,device) VALUES(?,?,?,?,?,?,?)",
                    (host, now.timestamp(), day, path, ref_host, visitor, device))
    return True


# ---- reporting ----
def stats(site: str, days: int = 30) -> dict:
    host = host_of(site)
    days = min(max(days, 1), 365)
    start_day = (date.today() - timedelta(days=days - 1)).isoformat()
    now = time.time()
    with _conn() as con:
        def q(sql, *a):
            return [dict(r) for r in con.execute(sql, a)]
        tot = q("SELECT COUNT(*) AS views FROM traffic_hits WHERE host=? AND day>=?", host, start_day)[0]
        # visitors are daily-hashed, so summing per-day uniques is the honest figure
        visitors = q("""SELECT COALESCE(SUM(n),0) AS v FROM (SELECT COUNT(DISTINCT visitor) AS n
                        FROM traffic_hits WHERE host=? AND day>=? GROUP BY day)""", host, start_day)[0]["v"]
        series = {r["day"]: r for r in q("""SELECT day, COUNT(*) AS views, COUNT(DISTINCT visitor) AS visitors
                                           FROM traffic_hits WHERE host=? AND day>=? GROUP BY day""", host, start_day)}
        daily = []
        for i in range(days):
            d = (date.today() - timedelta(days=days - 1 - i)).isoformat()
            daily.append({"day": d, "views": series.get(d, {}).get("views", 0),
                          "visitors": series.get(d, {}).get("visitors", 0)})
        live = q("SELECT COUNT(DISTINCT visitor) AS n FROM traffic_hits WHERE host=? AND ts>=?", host, now - 300)[0]["n"]
        return {
            "site": host, "days": days, "views": tot["views"], "visitors": visitors, "live_now": live,
            "daily": daily,
            "top_pages": q("""SELECT path, COUNT(*) AS views FROM traffic_hits WHERE host=? AND day>=?
                              GROUP BY path ORDER BY views DESC LIMIT 15""", host, start_day),
            "referrers": q("""SELECT ref_host AS source, COUNT(*) AS views FROM traffic_hits
                              WHERE host=? AND day>=? AND ref_host!='' GROUP BY ref_host ORDER BY views DESC LIMIT 15""",
                           host, start_day),
            "devices": q("""SELECT device, COUNT(*) AS views FROM traffic_hits WHERE host=? AND day>=?
                            GROUP BY device ORDER BY views DESC""", host, start_day),
        }


# ---- the embeddable script ----
def tracker_js(collect_url: str) -> str:
    return """(function(){
  var s=document.currentScript,site=(s&&s.getAttribute("data-site"))||location.hostname,last="";
  function send(){
    var p=location.pathname+location.search;if(p===last)return;last=p;
    var body=JSON.stringify({s:site,p:p,r:document.referrer});
    try{if(navigator.sendBeacon&&navigator.sendBeacon(%(u)r,new Blob([body],{type:"text/plain"})))return}catch(e){}
    try{fetch(%(u)r,{method:"POST",body:body,keepalive:true,mode:"no-cors"})}catch(e){}
  }
  var ps=history.pushState;
  if(ps){history.pushState=function(){ps.apply(this,arguments);setTimeout(send,0)};addEventListener("popstate",send)}
  if(document.visibilityState==="prerender"){addEventListener("visibilitychange",send,{once:true})}else send();
})();
""" % {"u": collect_url}
