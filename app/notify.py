"""Email notifications (SMTP). Configure SMTP_HOST, SMTP_PORT (587), SMTP_USER, SMTP_PASSWORD, SMTP_FROM."""
from __future__ import annotations

import mimetypes
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path


def smtp_configured() -> bool:
    return bool(os.getenv("SMTP_HOST"))


def send_email(to: str, subject: str, body: str, attachments: list[str] | None = None, html: str = "") -> None:
    if not smtp_configured():
        raise RuntimeError("SMTP not configured (set SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM)")
    msg = EmailMessage()
    msg["From"] = os.getenv("SMTP_FROM") or os.getenv("SMTP_USER") or "seo-suite@localhost"
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    for path in attachments or []:
        p = Path(path)
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        main, sub = ctype.split("/", 1)
        msg.add_attachment(p.read_bytes(), maintype=main, subtype=sub, filename=p.name)
    port = int(os.getenv("SMTP_PORT", "587"))
    cls = smtplib.SMTP_SSL if port == 465 else smtplib.SMTP
    with cls(os.environ["SMTP_HOST"], port, timeout=30) as s:
        if port != 465:
            try:
                s.starttls()
            except smtplib.SMTPException:
                pass
        if os.getenv("SMTP_USER"):
            s.login(os.environ["SMTP_USER"], os.getenv("SMTP_PASSWORD", ""))
        s.send_message(msg)


def crawl_digest(site: str, summary: dict, diff: dict | None) -> tuple[str, str]:
    """(subject, plain-text body) for a finished crawl, with changes since the previous crawl if available."""
    sev = {"critical": 0, "warning": 0, "info": 0}
    for i in summary.get("issues", []):
        sev[i["severity"]] += i["count"]
    lines = [f"SEO crawl finished for {site}", "",
             f"Technical score: {summary['score']['overall']}   GEO score: {summary.get('geo', {}).get('score', '-')}",
             f"Pages crawled: {summary['pages_crawled']}",
             f"Critical: {sev['critical']}   Warnings: {sev['warning']}   Info: {sev['info']}", ""]
    if diff:
        s = diff["summary"]
        lines += ["Changes since the previous crawl:",
                  f"  New URLs: {s['added']}   Removed URLs: {s['removed']}   Changed URLs: {s['changed_urls']}",
                  f"  Issue types worse: {s['issues_worse']}   better: {s['issues_better']}"]
        for c in [c for c in diff["issue_changes"] if c["delta"] > 0][:8]:
            lines.append(f"  ▲ {c['message']}: {c['old']} → {c['new']}")
        for c in [c for c in diff["issue_changes"] if c["delta"] < 0][:5]:
            lines.append(f"  ▼ {c['message']}: {c['old']} → {c['new']}")
    top = [i for i in summary.get("issues", []) if i["severity"] == "critical"][:8]
    if top:
        lines += ["", "Top critical issues:"] + [f"  - {i['message']} ({i['count']})" for i in top]
    changed = bool(diff and (diff["summary"]["issues_worse"] or diff["summary"]["added"] or diff["summary"]["removed"]))
    subject = f"[SEO] {site}: score {summary['score']['overall']}" + (" — issues changed" if changed else "")
    return subject, "\n".join(lines)
