"""Daily FC P&L monitor: ingest -> compute -> detect -> explain.

Business rules (from the brief)
  CM1 = Revenue - Manpower - Packaging            CM2 = CM1 - (Rent + Power&Fuel + Equipment + Overheads)
  Line item flagged when its % of revenue is outside its target range.
  CM1% flagged when outside 65-70 +-3pp (i.e. <62 or >73); CM2% when outside 15-30 +-3pp (<12 or >33).
  CM2 colour: Red <12 | Yellow 12-14.9 and 30-33 | Green 15-30 | Blue >33 (suspicious: under-reported costs?)
  The brief's thresholds leave small gaps (14.9-15, 30-30.1, 32.9-33); they are closed here:
  green is 15 <= CM2 <= 30, yellow fills 12 <= CM2 < 15 and 30 < CM2 <= 33, blue is CM2 > 33.

Sample workbook layout (Case Study 3.xlsx)
  tab 1 "Ideal FC Criteria": rows like  'Power and Fuel' | '10 - 12%'   (the real target ranges)
  tab 2 "Day Wise PnL":      Day | Revenue | Manpower | Packaging | Power and Fuel | FC Rent | Equipment Rentals | Overheads
  'Day' holds day numbers 1..N, not calendar dates, so they are mapped to dates starting at start_date.
"""

import csv
import json
import os
import re
import statistics
import urllib.request
from datetime import date, datetime, timedelta

from openpyxl import load_workbook

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LINE_ITEMS = [
    "Manpower Cost",
    "Packaging Cost",
    "Power & Fuel",
    "FC Rent",
    "Equipment Rentals",
    "Overheads",
]
KEYS = {
    "Manpower Cost": "manpower",
    "Packaging Cost": "packaging",
    "Power & Fuel": "power_fuel",
    "FC Rent": "fc_rent",
    "Equipment Rentals": "equipment",
    "Overheads": "overheads",
}
CM1_TARGET, CM2_TARGET, DEV_PP = (65.0, 70.0), (15.0, 30.0), 3.0
RED_BELOW, BLUE_ABOVE = CM2_TARGET[0] - DEV_PP, CM2_TARGET[1] + DEV_PP  # 12 and 33
CM1_LOW, CM1_HIGH = CM1_TARGET[0] - DEV_PP, CM1_TARGET[1] + DEV_PP  # 62 and 73

# day 1 = 20 Nov 2026, day 27 = 16 Dec 2026, day 30 = 19 Dec 2026
DEFAULT_START_DATE = date(2026, 11, 20)

# Ranges from the sample sheet's first tab ("Ideal FC Criteria"). They are still auto-detected from the
# uploaded workbook / targets file and override these; these are only the fallback for plain CSVs.
DEFAULT_TARGETS = {
    "Manpower Cost": (25.0, 27.0),
    "Packaging Cost": (5.0, 7.0),
    "Power & Fuel": (10.0, 12.0),
    "FC Rent": (15.0, 18.0),
    "Equipment Rentals": (5.0, 8.0),
    "Overheads": (10.0, 12.0),
}
ACTIONS = {
    "Manpower Cost": "check headcount, overtime and attendance against the day's volume",
    "Packaging Cost": "check packaging consumption per order and any rate or wastage change",
    "Power & Fuel": "check DG/grid usage, meter readings and fuel purchases",
    "FC Rent": "check whether the amount booked is correct (rent is fixed, so a high % usually means low revenue)",
    "Equipment Rentals": "check rental invoices and idle equipment",
    "Overheads": "review overhead bookings for one-offs or misposted entries",
}


# ---------- helpers ----------
def _norm(h) -> str:
    return re.sub(r"[^a-z0-9]", "", str(h or "").lower())


def canon(h):
    """Map a header / label to a canonical field name, or None."""
    n = _norm(h)
    if not n:
        return None
    if n in (
        "fc",
        "fcname",
        "centre",
        "center",
        "warehouse",
        "site",
        "location",
        "fccode",
    ):
        return "fc"
    if n in ("day", "date") or n.startswith("day") and len(n) <= 5:
        return "day"
    if "revenue" in n or n == "sales":
        return "revenue"
    if "manpower" in n or "labour" in n or "labor" in n:
        return "Manpower Cost"
    if "packag" in n:
        return "Packaging Cost"
    if "power" in n or "fuel" in n:
        return "Power & Fuel"
    if "equip" in n:
        return "Equipment Rentals"
    if "rent" in n:
        return "FC Rent"
    if "overhead" in n:
        return "Overheads"
    return None


def _num(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(re.sub(r"[,\u20b9$%\s]", "", str(v)))
    except ValueError:
        return None


def _day(v):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, (int, float)) and 30000 < v < 80000:  # excel serial
        return (datetime(1899, 12, 30) + timedelta(days=float(v))).date()
    s = str(v or "").strip()
    for fmt in (
        "%Y-%m-%d",
        "%d-%m-%Y",
        "%d/%m/%Y",
        "%d-%b-%Y",
        "%d %b %Y",
        "%d-%b-%y",
        "%m/%d/%Y",
        "%Y/%m/%d",
    ):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def _day_number(v):
    """'Day' cells such as 1, 1.0, '27' -> 1, 27. Anything else -> None."""
    if isinstance(v, (datetime, date)):
        return None
    n = _num(v)
    if n is None or n != int(n) or not 1 <= n <= 3660:
        return None
    return int(n)


def fmt_amt(x: float) -> str:
    return f"\u20b9{x:,.0f}"


def fmt_day(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d:%b} {d.day}"


# ---------- Tool 1: ingest ----------
def _sheets(path: str):
    if path.lower().endswith(".csv"):
        with open(path, newline="", encoding="utf-8-sig") as f:
            return [(os.path.splitext(os.path.basename(path))[0], list(csv.reader(f)))]
    wb = load_workbook(path, data_only=True, read_only=True)
    return [
        (ws.title, [list(r) for r in ws.iter_rows(values_only=True)])
        for ws in wb.worksheets
    ]


def find_targets(sheets) -> dict:
    """Look for rows like 'Power and Fuel | 10 - 12%' (or two separate min/max cells) anywhere in the workbook."""
    found = {}
    for _, rows in sheets:
        for r in rows:
            for i, cell in enumerate(r):
                li = canon(cell)
                if li in LINE_ITEMS and li not in found:
                    nums = []
                    for c in r[i + 1 :]:
                        if isinstance(c, (int, float)):
                            nums.append(float(c))
                        elif isinstance(c, str):
                            nums += [float(x) for x in re.findall(r"\d+(?:\.\d+)?", c)]
                    if len(nums) >= 2 and nums[0] < nums[1]:
                        lo, hi = nums[0], nums[1]
                        if hi <= 1.0:  # stored as fractions (0.08, 0.14)
                            lo, hi = lo * 100, hi * 100
                        found[li] = (lo, hi)
                    break
    return found if len(found) >= 4 else {}


def load_targets(path=None, sheets=None) -> tuple[dict, str]:
    """Priority: uploaded targets file > targets inside the data workbook > config/targets.json > defaults."""
    if path:
        t = find_targets(_sheets(path))
        if not t:
            raise ValueError(
                "Targets file: could not find at least 4 line items with a min and max %."
            )
        return {**DEFAULT_TARGETS, **t}, "uploaded targets file"
    if sheets:
        t = find_targets(sheets)
        if t:
            return {**DEFAULT_TARGETS, **t}, "targets found in the data workbook"
    cfg = os.path.join(ROOT, "config", "targets.json")
    if os.path.exists(cfg):
        with open(cfg) as f:
            return {
                **DEFAULT_TARGETS,
                **{k: tuple(v) for k, v in json.load(f).items() if k in LINE_ITEMS},
            }, "config/targets.json"
    return dict(DEFAULT_TARGETS), "sample-sheet defaults"


def read_data(
    path: str, fc_name: str = "", start_date: str = ""
) -> tuple[list[dict], list[str], list]:
    """Returns (records, issues, sheets). One record per (FC, day).

    If the Day column holds day numbers (1, 2, 3 ...) they become dates: day 1 = start_date
    (default 1 May 2025), day n = start_date + n-1 days.
    """
    sheets = _sheets(path)
    start = (_day(start_date) if start_date.strip() else None) or DEFAULT_START_DATE
    data_sheets = []
    for name, rows in sheets:
        for hi, r in enumerate(rows[:30]):
            cols = {}
            for i, h in enumerate(r):
                k = canon(h)
                if k and k not in cols:
                    cols[k] = i
            if "day" in cols and "revenue" in cols:
                data_sheets.append((name, rows, hi, cols))
                break
    if not data_sheets:
        raise ValueError(
            "No table with 'Day' and 'Revenue' columns found. Expected: Day, Revenue, Manpower, "
            "Packaging, Power and Fuel, FC Rent, Equipment Rentals, Overheads."
        )
    default_fc = fc_name.strip() or (
        data_sheets[0][0] if len(data_sheets) > 1 else "FC-1"
    )
    records, issues, numbered = {}, [], False
    for name, rows, hi, cols in data_sheets:
        missing_cols = [li for li in LINE_ITEMS if li not in cols]
        if missing_cols:
            issues.append(
                f"Sheet '{name}': no column for {', '.join(missing_cols)}; treated as not reported (0)."
            )
        for rn, r in enumerate(rows[hi + 1 :], start=hi + 2):
            if not any(c not in (None, "") for c in r):
                continue
            get = lambda k: r[cols[k]] if k in cols and cols[k] < len(r) else None
            raw = get("day")
            d = _day(raw)
            if d is None:
                n = _day_number(raw)
                if n is not None:
                    d, numbered = start + timedelta(days=n - 1), True
            if d is None:
                issues.append(
                    f"Sheet '{name}' row {rn}: unreadable date '{raw}', row skipped."
                )
                continue
            rev = _num(get("revenue"))
            if not rev or rev <= 0:
                issues.append(
                    f"Sheet '{name}' row {rn} ({d}): revenue missing or zero, so percentages cannot be computed; row skipped."
                )
                continue
            fc = (
                str(get("fc")).strip()
                if get("fc") not in (None, "")
                else (name if len(data_sheets) > 1 else default_fc)
            )
            costs, unreported = {}, []
            for li in LINE_ITEMS:
                v = _num(get(li))
                if v is None:
                    unreported.append(li)
                    v = 0.0
                costs[li] = v
            if (fc, d) in records:
                issues.append(
                    f"{fc} {d}: appears more than once; the last row was used."
                )
            records[(fc, d)] = {
                "fc": fc,
                "day": d.isoformat(),
                "revenue": rev,
                "costs": costs,
                "unreported": unreported,
            }
    if numbered:
        issues.insert(
            0,
            f"The Day column holds day numbers, not dates: Day 1 was mapped to {start:%d %b %Y} "
            "(pass start_date to change it).",
        )
    if not records:
        raise ValueError(
            "The table has no usable rows (every row had a bad date or zero/missing revenue)."
        )
    return sorted(records.values(), key=lambda x: (x["fc"], x["day"])), issues, sheets


# ---------- Tool 2: compute ----------
def colour_for(cm2: float) -> str:
    if cm2 < RED_BELOW:
        return "RED"
    if cm2 > BLUE_ABOVE:
        return "BLUE"
    if CM2_TARGET[0] <= cm2 <= CM2_TARGET[1]:
        return "GREEN"
    return "YELLOW"


def compute(rec: dict, targets: dict) -> dict:
    rev, c = rec["revenue"], rec["costs"]
    cm1 = rev - c["Manpower Cost"] - c["Packaging Cost"]
    cm2 = (
        cm1 - c["FC Rent"] - c["Power & Fuel"] - c["Equipment Rentals"] - c["Overheads"]
    )
    day = {
        "fc": rec["fc"],
        "day": rec["day"],
        "revenue": rev,
        "cm1": cm1,
        "cm1_pct": cm1 / rev * 100,
        "cm2": cm2,
        "cm2_pct": cm2 / rev * 100,
        "unreported": rec["unreported"],
    }
    for li in LINE_ITEMS:
        day[KEYS[li]] = c[li]
        day[KEYS[li] + "_pct"] = c[li] / rev * 100
    day["colour"] = colour_for(day["cm2_pct"])
    return day


# ---------- Tool 3: detect ----------
def detect(day: dict, targets: dict) -> list[dict]:
    """Flags sorted by impact. Each: line_item, pct, target, status, pp, impact (rupees, cost lines only)."""
    flags = []
    for li in LINE_ITEMS:
        p, (lo, hi) = day[KEYS[li] + "_pct"], targets[li]
        if p > hi:
            flags.append(
                {
                    "line_item": li,
                    "pct": p,
                    "target": f"{lo:g}-{hi:g}%",
                    "status": "ABOVE MAX",
                    "pp": p - hi,
                    "impact": (p - hi) / 100 * day["revenue"],
                }
            )
        elif p < lo:
            note = " (not reported)" if li in day["unreported"] else ""
            flags.append(
                {
                    "line_item": li,
                    "pct": p,
                    "target": f"{lo:g}-{hi:g}%",
                    "status": "BELOW MIN" + note,
                    "pp": lo - p,
                    "impact": 0.0,
                }
            )
    if day["cm1_pct"] < CM1_LOW or day["cm1_pct"] > CM1_HIGH:
        low = day["cm1_pct"] < CM1_LOW
        flags.append(
            {
                "line_item": "CM1 %",
                "pct": day["cm1_pct"],
                "target": f"{CM1_TARGET[0]:g}-{CM1_TARGET[1]:g}% (+-{DEV_PP:g}pp)",
                "status": "BELOW LIMIT" if low else "ABOVE LIMIT",
                "pp": (CM1_LOW - day["cm1_pct"]) if low else day["cm1_pct"] - CM1_HIGH,
                "impact": 0.0,
            }
        )
    if day["colour"] in ("RED", "BLUE"):
        red = day["colour"] == "RED"
        flags.append(
            {
                "line_item": "CM2 %",
                "pct": day["cm2_pct"],
                "target": f"{CM2_TARGET[0]:g}-{CM2_TARGET[1]:g}% (+-{DEV_PP:g}pp)",
                "status": (
                    "BELOW LIMIT (RED)" if red else "ABOVE LIMIT (BLUE, suspicious)"
                ),
                "pp": (
                    (RED_BELOW - day["cm2_pct"]) if red else day["cm2_pct"] - BLUE_ABOVE
                ),
                "impact": 0.0,
            }
        )
    order = {"CM2 %": 0, "CM1 %": 1}
    flags.sort(key=lambda f: (order.get(f["line_item"], 2), -f["impact"], -f["pp"]))
    return flags


# ---------- Tool 4: explain ----------
def explain(
    day: dict, flags: list[dict], targets: dict, recent_revenue: list[float]
) -> str:
    if not flags:
        return ""
    cm2, cm1, col = day["cm2_pct"], day["cm1_pct"], day["colour"]
    parts = []
    if col == "RED":
        parts.append(
            f"CM2 = {cm2:.1f}% (below the {RED_BELOW:g}% red threshold, {RED_BELOW - cm2:.1f} pp under; target {CM2_TARGET[0]:g}-{CM2_TARGET[1]:g}%)."
        )
    elif col == "BLUE":
        parts.append(
            f"CM2 = {cm2:.1f}% (above {BLUE_ABOVE:g}%, suspiciously high; target {CM2_TARGET[0]:g}-{CM2_TARGET[1]:g}%)."
        )
    else:
        parts.append(
            f"CM2 = {cm2:.1f}% ({'within target' if col == 'GREEN' else 'in the watch zone'})."
        )
    cm1_flag = any(f["line_item"] == "CM1 %" for f in flags)
    parts.append(
        f"CM1 = {cm1:.1f}% ({'outside' if cm1_flag else 'within'} the {CM1_TARGET[0]:g}-{CM1_TARGET[1]:g}% target band)."
    )

    over = [f for f in flags if f["status"] == "ABOVE MAX"]
    under = [f for f in flags if f["status"].startswith("BELOW MIN")]
    if over:
        items = ", ".join(
            f"{f['line_item']} {f['pct']:.1f}% (max {targets[f['line_item']][1]:g}%, +{f['pp']:.1f} pp, about {fmt_amt(f['impact'])})"
            for f in over
        )
        parts.append(f"Above max, largest impact first: {items}.")
    if under:
        items = ", ".join(
            f"{f['line_item']} {f['pct']:.1f}% (min {targets[f['line_item']][0]:g}%{', not reported' if 'not reported' in f['status'] else ''})"
            for f in under
        )
        parts.append(
            f"Below min: {items}"
            + (" - possible under-reporting." if col == "BLUE" else ".")
        )
    if col == "RED" and not over:
        near = sorted(
            LINE_ITEMS,
            key=lambda li: day[KEYS[li] + "_pct"] - targets[li][1],
            reverse=True,
        )[:2]
        parts.append(
            "No single line item breached its range; several lines are running close to their maximums "
            "(closest: "
            + ", ".join(
                f"{li} {day[KEYS[li] + '_pct']:.1f}% vs max {targets[li][1]:g}%"
                for li in near
            )
            + ")."
        )
    if day["unreported"]:
        parts.append(
            f"No value was supplied for {', '.join(day['unreported'])}; it was treated as 0."
        )
    if len(recent_revenue) >= 3:
        med = statistics.median(recent_revenue)
        if med and day["revenue"] < 0.8 * med:
            parts.append(
                f"Revenue {fmt_amt(day['revenue'])} is {100 - day['revenue'] / med * 100:.0f}% below this FC's recent median, which inflates fixed-cost percentages."
            )

    if col == "RED":
        top = [f["line_item"] for f in over[:2]]
        act = (
            "Recommend a cost audit"
            + (
                f", starting with {' and '.join(top)}: "
                + "; ".join(ACTIONS[t] for t in top)
                if top
                else ""
            )
            + "."
        )
    elif col == "BLUE":
        names = [f["line_item"] for f in under] or ["all cost lines"]
        act = f"Recommend verifying cost booking before trusting this margin: confirm {', '.join(names)} are complete for the day."
    elif over or under:
        names = [f["line_item"] for f in over + under]
        act = f"Recommend reviewing {', '.join(names)} with the FC manager; margin is still {'within target' if col == 'GREEN' else 'in the watch zone'}."
    else:
        act = "Recommend checking manpower and packaging, since CM1 left its band."
    parts.append(act)
    text = f"On {fmt_day(day['day'])}, " + " ".join(parts)
    return polish(text)


def polish(text: str) -> str:
    """Optional LLM step: if a local Ollama model is configured, let it make the text more readable.
    It must keep every number; otherwise the deterministic text is used. No paid API needed.
    """
    model = os.getenv("OLLAMA_MODEL")
    if not model:
        return text
    try:
        body = json.dumps(
            {
                "model": model,
                "stream": False,
                "prompt": "Rewrite this P&L alert as 2-3 short plain sentences for a warehouse manager. "
                "Keep every number and every recommendation exactly. Output only the text.\n\n"
                + text,
            }
        ).encode()
        req = urllib.request.Request(
            os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate"),
            body,
            {"Content-Type": "application/json"},
        )
        out = json.loads(urllib.request.urlopen(req, timeout=30).read())[
            "response"
        ].strip()
        nums = lambda s: sorted(re.findall(r"\d+(?:\.\d+)?", s.replace(",", "")))
        return out if out and all(n in nums(out) for n in nums(text)) else text
    except Exception:
        return text


# ---------- Orchestrator ----------
def analyze(
    records: list[dict], targets: dict, prior: dict | None = None
) -> tuple[list[dict], list[dict]]:
    """prior = {fc: [(day_iso, revenue), ...]} from earlier uploads (memory of past days).
    Returns (days, anomalies). Each day carries its insight; each anomaly row repeats it.
    """
    prior = prior or {}
    hist = {fc: {d: r for d, r in v} for fc, v in prior.items()}
    for r in records:
        hist.setdefault(r["fc"], {})[r["day"]] = r["revenue"]
    days, anomalies = [], []
    for rec in records:
        day = compute(rec, targets)
        flags = detect(day, targets)
        earlier = sorted(d for d in hist[rec["fc"]] if d < rec["day"])[-7:]
        day["insight"] = explain(
            day, flags, targets, [hist[rec["fc"]][d] for d in earlier]
        )
        days.append(day)
        for f in flags:
            anomalies.append(
                {
                    "day": day["day"],
                    "fc": day["fc"],
                    "line_item": f["line_item"],
                    "pct": round(f["pct"], 2),
                    "target_range": f["target"],
                    "status": f["status"],
                    "insight": day["insight"],
                }
            )
    return days, anomalies
