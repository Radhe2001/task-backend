"""Daily digest per FC, delivered to the FC's manager.

Channels (all free):
  outbox  always: writes data/outbox/<day>_<fc>.html and .txt, so a run is verifiable with no accounts
  email   when SMTP_HOST is set (Gmail app password, or a local Mailpit/MailHog on localhost:1025)
  slack   when the manager has a slack_webhook in config/managers.json, or SLACK_WEBHOOK_URL is set
Secrets only come from environment variables / the git-ignored managers.json; none are committed.
"""

import html
import json
import os
import smtplib
import urllib.request
from email.message import EmailMessage
from app import db
from .agent import fmt_amt, fmt_day

OUTBOX = os.path.join(db.ROOT, "data", "outbox")
CFG = os.path.join(db.ROOT, "config", "managers.json")
ICON = {
    "RED": "\U0001f534",
    "YELLOW": "\U0001f7e1",
    "GREEN": "\U0001f7e2",
    "BLUE": "\U0001f535",
}
HEX = {"RED": "#f8b4b4", "YELLOW": "#fde68a", "GREEN": "#b7e4c7", "BLUE": "#b6d4fe"}
MEANING = {
    "RED": "CM2 below 12%",
    "YELLOW": "CM2 in the watch zone",
    "GREEN": "CM2 within target",
    "BLUE": "CM2 above 33%: check for under-reported costs",
}


def managers() -> dict:
    if os.path.exists(CFG):
        with open(CFG) as f:
            return json.load(f)
    return {}


def manager_for(fc: str) -> dict:
    m = managers()
    return {**m.get("default", {}), **m.get(fc, {})}


def build_digest(fc: str, day: dict, anomalies: list[dict], trend: list[dict]):
    col = day["colour"]
    head = f"{ICON[col]} {fc} | {fmt_day(day['day'])} | {col}: {MEANING[col]}"
    lines = [
        head,
        f"Revenue {fmt_amt(day['revenue'])} | CM1 {day['cm1_pct']:.1f}% | CM2 {day['cm2_pct']:.1f}% ({fmt_amt(day['cm2'])})",
        "Last days: " + " ".join(ICON[t["colour"]] for t in trend),
    ]
    if anomalies:
        lines.append("")
        lines.append(f"{len(anomalies)} flag(s):")
        lines += [
            f"- {a['line_item']}: {a['pct']:.1f}% (target {a['target_range']}) {a['status']}"
            for a in anomalies
        ]
        lines += ["", day["insight"]]
    else:
        lines += ["", "No flags today."]
    text = "\n".join(lines)
    rows = "".join(
        f"<tr><td>{html.escape(a['line_item'])}</td><td>{a['pct']:.1f}%</td><td>{html.escape(a['target_range'])}</td>"
        f"<td>{html.escape(a['status'])}</td></tr>"
        for a in anomalies
    )
    body = (
        f"<div style='font-family:sans-serif;max-width:640px'><div style='background:{HEX[col]};padding:12px;border-radius:6px'>"
        f"<b>{html.escape(fc)} | {fmt_day(day['day'])}</b><br>{col}: {MEANING[col]}</div>"
        f"<p>Revenue {fmt_amt(day['revenue'])} &middot; CM1 {day['cm1_pct']:.1f}% &middot; CM2 {day['cm2_pct']:.1f}%</p>"
        f"<p>Last days: {' '.join(ICON[t['colour']] for t in trend)}</p>"
    )
    if anomalies:
        body += (
            f"<table border='1' cellpadding='6' style='border-collapse:collapse'><tr><th>Line item</th><th>% of revenue</th>"
            f"<th>Target</th><th>Status</th></tr>{rows}</table><p>{html.escape(day['insight'])}</p>"
        )
    else:
        body += "<p>No flags today.</p>"
    return f"{head}", text, body + "</div>"


def deliver(fc: str, day_iso: str) -> list[dict]:
    day = db.get_day(fc, day_iso)
    if not day:
        return [
            {
                "fc": fc,
                "day": day_iso,
                "channel": "-",
                "status": "skipped",
                "detail": "no data for that FC and day",
            }
        ]
    subject, text, body = build_digest(
        fc, day, db.get_anomalies(fc, day_iso), db.fc_trend(fc, day_iso)
    )
    mgr = manager_for(fc)
    results = []

    def record(channel, recipient, status, detail=""):
        db.log_notification(day_iso, fc, channel, recipient, status, detail)
        results.append(
            {
                "fc": fc,
                "day": day_iso,
                "channel": channel,
                "recipient": recipient,
                "status": status,
                "detail": detail,
            }
        )

    os.makedirs(OUTBOX, exist_ok=True)
    stem = os.path.join(
        OUTBOX, f"{day_iso}_{''.join(ch if ch.isalnum() else '_' for ch in fc)}"
    )
    with open(stem + ".html", "w", encoding="utf-8") as f:
        f.write(body)
    with open(stem + ".txt", "w", encoding="utf-8") as f:
        f.write(text)
    record("outbox", os.path.relpath(stem + ".html", db.ROOT), "saved")

    host, to = os.getenv("SMTP_HOST"), mgr.get("email")
    if host and to:
        try:
            msg = EmailMessage()
            msg["Subject"], msg["From"], msg["To"] = (
                subject,
                os.getenv("SMTP_FROM", "fc-monitor@localhost"),
                to,
            )
            msg.set_content(text)
            msg.add_alternative(body, subtype="html")
            with smtplib.SMTP(
                host, int(os.getenv("SMTP_PORT", "587")), timeout=20
            ) as s:
                if os.getenv("SMTP_TLS", "1") == "1":
                    s.starttls()
                if os.getenv("SMTP_USER"):
                    s.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASS", ""))
                s.send_message(msg)
            record("email", to, "sent")
        except Exception as e:
            record("email", to, "failed", f"{type(e).__name__}: {str(e)[:150]}")
    hook = mgr.get("slack_webhook") or os.getenv("SLACK_WEBHOOK_URL")
    if hook:
        try:
            req = urllib.request.Request(
                hook,
                json.dumps({"text": text}).encode(),
                {"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=15).read()
            record("slack", mgr.get("name", "channel"), "sent")
        except Exception as e:
            record(
                "slack",
                mgr.get("name", "channel"),
                "failed",
                f"{type(e).__name__}: {str(e)[:150]}",
            )
    return results


def send_digests(
    day_iso: str | None = None, fcs: list[str] | None = None
) -> list[dict]:
    """Send the digest for one day (default: each FC's latest day) to every FC's manager."""
    with db.conn() as c:
        names = fcs or [
            r["fc"] for r in c.execute("SELECT DISTINCT fc FROM days ORDER BY fc")
        ]
    out = []
    for fc in names:
        out += deliver(fc, day_iso or db.latest_day(fc))
    return out
