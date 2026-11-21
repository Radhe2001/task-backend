"""Ingest -> compute -> detect -> explain -> store -> notify. Used by the API and by run_daily.py."""

from collections import Counter

from . import agent, notifier
from app import db


def ingest(
    path: str,
    fc_name: str = "",
    targets_path: str | None = None,
    job_id: str | None = None,
    notify: bool = True,
    progress=None,
    start_date: str = "",
) -> dict:
    say = progress or (lambda stage, pct: None)
    say("Reading file", 5)
    records, issues, sheets = agent.read_data(path, fc_name, start_date)
    targets, targets_source = agent.load_targets(targets_path, sheets)
    say("Computing P&L and checking limits", 30)
    prior = db.prior_revenue(sorted({r["fc"] for r in records}))
    days, anomalies = agent.analyze(records, targets, prior)
    say("Saving", 80)
    db.save_days(job_id or "cli", days, anomalies)
    sent = []
    if notify:
        say("Sending digests", 90)
        latest = {}
        for d in days:
            latest[d["fc"]] = max(latest.get(d["fc"], ""), d["day"])
        for fc, day in latest.items():
            sent += notifier.deliver(fc, day)
    return {
        "days": len(days),
        "fcs": sorted({d["fc"] for d in days}),
        "anomalies": len(anomalies),
        "colours": dict(Counter(d["colour"] for d in days)),
        "targets_source": targets_source,
        "issues": issues[:50],
        "issue_count": len(issues),
        "digests": len(sent),
    }
