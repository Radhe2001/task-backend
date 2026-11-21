"""Wagesheet vs ECR verification: read wagesheet, verify per employee, summarise, export.

Verdicts per employee, separately for PF and ESIC:
  MATCHED         identifier found, name matches (see matching.py), amount within tolerance
  NAME_MISMATCH   identifier found, name differs (amount difference, if any, is noted too)
  AMOUNT_MISMATCH identifier + name match, amount differs (both values and difference shown)
  MISSING         identifier not in the ECR (or deduction made but no identifier in the wagesheet)
  NOT_APPLICABLE  nothing deducted in the wagesheet and nothing to find (e.g. salary above ESIC limit)
  REVIEW          found in ECR but the amount could not be read from the PDF
  NOT_CHECKED     that ECR was not uploaded
"""

import csv
import io
import re
from collections import Counter

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from .matching import compare_names

AMOUNT_TOL = 1.0  # rupees. ESIC rounds up to the next rupee, so +-1 is rounding, not a mismatch
STATUSES = ["MATCHED", "NAME_MISMATCH", "AMOUNT_MISMATCH", "MISSING", "NOT_APPLICABLE", "REVIEW", "NOT_CHECKED"]
COLOURS = {
    "MATCHED": "C6EFCE", "NAME_MISMATCH": "FFEB9C", "AMOUNT_MISMATCH": "FFC7CE", "MISSING": "F4B084",
    "NOT_APPLICABLE": "E7E6E6", "REVIEW": "DDEBF7", "NOT_CHECKED": "E7E6E6",
}


# ---------- Tool 1: read the wagesheet ----------
def _norm(h) -> str:
    return re.sub(r"[^a-z0-9]", "", str(h or "").lower())


def _num(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(re.sub(r"[,\u20b9\s]", "", str(v)))
    except ValueError:
        return None


def _digits(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return re.sub(r"\D", "", str(v))


def _map_headers(row) -> dict:
    m = {}
    for i, h in enumerate(row):
        n = _norm(h)
        if not n:
            continue
        if "esic" in n:
            m.setdefault("esic", i)
        elif "uan" in n:
            m.setdefault("uan", i)
        elif n.startswith("ip"):
            m.setdefault("ip", i)
        elif n.startswith("pf") or n.startswith("epf"):
            m.setdefault("pf", i)
        elif n.startswith("name") or n in ("employeename", "empname"):
            m.setdefault("name", i)
        elif n.startswith("agency") or n.startswith("vendor"):
            m.setdefault("agency", i)
        elif n.startswith("state"):
            m.setdefault("state", i)
    return m


def read_wagesheet(path: str) -> tuple[list[dict], list[str]]:
    if path.lower().endswith(".csv"):
        with open(path, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
    else:
        wb = load_workbook(path, data_only=True, read_only=True)
        rows = []
        for ws in wb.worksheets:
            rows = [list(r) for r in ws.iter_rows(values_only=True)]
            if any(len(_map_headers(r)) >= 3 and "uan" in _map_headers(r) for r in rows[:20]):
                break
    hdr_i, cols = None, {}
    for i, r in enumerate(rows[:20]):
        m = _map_headers(r)
        if "uan" in m and "name" in m:
            hdr_i, cols = i, m
            break
    if hdr_i is None:
        raise ValueError("Wagesheet needs a header row with at least 'Name' and 'UAN' columns.")
    missing = [c for c in ("pf", "esic", "ip") if c not in cols]
    warnings = [f"Wagesheet has no '{c}' column; that part is treated as blank." for c in missing]

    def cell(r, key):
        i = cols.get(key)
        return r[i] if i is not None and i < len(r) else None

    out, seen = [], Counter()
    for n, r in enumerate(rows[hdr_i + 1:], start=hdr_i + 2):
        name = str(cell(r, "name") or "").strip()
        uan, ip = _digits(cell(r, "uan")), _digits(cell(r, "ip"))
        if not (name or uan or ip) or name.lower().startswith(("total", "grand total")):
            continue
        out.append({
            "row_no": n, "name": name, "uan": uan, "ip": ip,
            "agency": str(cell(r, "agency") or "").strip(), "state": str(cell(r, "state") or "").strip(),
            "pf_amt": _num(cell(r, "pf")), "esic_amt": _num(cell(r, "esic")),
        })
        for k in (uan, ip):
            if k:
                seen[k] += 1
    dups = [k for k, c in seen.items() if c > 1]
    if dups:
        warnings.append(f"{len(dups)} identifier(s) appear on more than one wagesheet row (e.g. {dups[0]}).")
    if not out:
        raise ValueError("Wagesheet has a header but no employee rows.")
    return out, warnings


# ---------- Tool 2: verify one employee against one ECR ----------
def _fmt(x) -> str:
    return f"{x:,.2f}".rstrip("0").rstrip(".")


def check(kind: str, name: str, ident: str, amt, ecr) -> dict:
    """kind is 'PF' or 'ESIC'. Returns status, reason, ecr_name, ecr_amount, difference."""
    label = "UAN" if kind == "PF" else "IP Number"
    res = {"status": "", "reason": "", "ecr_name": "", "ecr_amount": None, "difference": None}
    amt = amt or 0.0
    if ecr is None:
        res.update(status="NOT_CHECKED", reason=f"{kind} ECR not uploaded")
        return res
    if not ident:
        if amt:
            res.update(status="MISSING", reason=f"No {label} in the wagesheet but {_fmt(amt)} was deducted")
        else:
            res.update(status="NOT_APPLICABLE", reason=f"No {label} and no {kind} deduction in the wagesheet")
        return res
    rec = ecr.get(ident)
    if rec is None:
        if amt:
            res.update(status="MISSING", reason=f"{label} {ident} not found in the {kind} ECR (wagesheet deducted {_fmt(amt)})")
        else:
            res.update(status="NOT_APPLICABLE", reason=f"No {kind} deduction in the wagesheet and {label} not in the ECR")
        return res

    res["ecr_name"], res["ecr_amount"] = rec["name"], rec["amount"]
    ok, score, note = compare_names(name, rec["name"])
    if rec["amount"] is None:
        res.update(status="REVIEW", reason="Found in the ECR but the amount could not be read from the PDF")
        return res
    diff = round(rec["amount"] - amt, 2)
    res["difference"] = diff
    amount_ok = abs(diff) <= AMOUNT_TOL
    extra = f" (listed {rec['count']} times in the ECR, amounts added)" if rec["count"] > 1 else ""
    if not ok:
        reason = f"Name differs: wagesheet '{name}' vs ECR '{rec['name']}' (similarity {score:.0%})"
        if not amount_ok:
            reason += f"; amount also differs: wagesheet {_fmt(amt)} vs ECR {_fmt(rec['amount'])}"
        res.update(status="NAME_MISMATCH", reason=reason + extra)
    elif not amount_ok:
        res.update(
            status="AMOUNT_MISMATCH",
            reason=f"Amount differs: wagesheet {_fmt(amt)} vs ECR {_fmt(rec['amount'])} (difference {diff:+,.2f})" + extra,
        )
    else:
        tail = "" if note == "identical" else f"; name {note}"
        res.update(status="MATCHED", reason=f"{label} found, amount {_fmt(amt)} matches" + tail + extra)
    return res


def verify(sheet_rows: list[dict], pf, esic) -> tuple[list[dict], list[dict]]:
    """pf / esic are {id: rec} maps (or None if not uploaded). Returns (results, extras)."""
    results = []
    for r in sheet_rows:
        p = check("PF", r["name"], r["uan"], r["pf_amt"], pf)
        e = check("ESIC", r["name"], r["ip"], r["esic_amt"], esic)
        results.append({
            "row_no": r["row_no"], "name": r["name"], "uan": r["uan"], "ip_number": r["ip"],
            "agency": r["agency"], "state": r["state"],
            "pf_wagesheet_amount": r["pf_amt"], "pf_ecr_name": p["ecr_name"], "pf_ecr_amount": p["ecr_amount"],
            "pf_difference": p["difference"], "pf_status": p["status"], "pf_reason": p["reason"],
            "esic_wagesheet_amount": r["esic_amt"], "esic_ecr_name": e["ecr_name"], "esic_ecr_amount": e["ecr_amount"],
            "esic_difference": e["difference"], "esic_status": e["status"], "esic_reason": e["reason"],
        })
    extras = []
    for kind, ecr, key in (("PF", pf, "uan"), ("ESIC", esic, "ip")):
        if ecr is None:
            continue
        listed = {r[key] for r in sheet_rows if r[key]}
        for ident, rec in ecr.items():
            if ident not in listed:
                extras.append({"kind": kind, "identifier": ident, "name": rec["name"], "amount": rec["amount"]})
    return results, extras


def summarize(results: list[dict]) -> dict:
    s = {"employees": len(results)}
    for kind in ("pf", "esic"):
        c = Counter(r[f"{kind}_status"] for r in results)
        s[kind] = {st: c.get(st, 0) for st in STATUSES}
        s[kind]["deducted_total"] = round(sum(r[f"{kind}_wagesheet_amount"] or 0 for r in results), 2)
        s[kind]["deposited_total_for_listed"] = round(sum(r[f"{kind}_ecr_amount"] or 0 for r in results), 2)
    return s


# ---------- Tool 3: export ----------
RESULT_HEADERS = [
    ("row_no", "Wagesheet row"), ("name", "Name"), ("uan", "UAN"), ("ip_number", "IP Number"),
    ("agency", "Agency"), ("state", "State"),
    ("pf_status", "PF status"), ("pf_reason", "PF reason"), ("pf_wagesheet_amount", "PF wagesheet"),
    ("pf_ecr_amount", "PF ECR (EE share)"), ("pf_difference", "PF difference"), ("pf_ecr_name", "PF ECR name"),
    ("esic_status", "ESIC status"), ("esic_reason", "ESIC reason"), ("esic_wagesheet_amount", "ESIC wagesheet"),
    ("esic_ecr_amount", "ESIC ECR (IP contrib.)"), ("esic_difference", "ESIC difference"), ("esic_ecr_name", "ESIC ECR name"),
]


def build_workbook(results: list[dict], extras: list[dict], summary: dict, warnings: list[str]) -> bytes:
    wb = Workbook()
    bold = Font(bold=True)

    ws = wb.active
    ws.title = "Summary"
    ws.append(["Status", "PF", "ESIC"])
    for c in ws[1]:
        c.font = bold
    for st in STATUSES:
        ws.append([st, summary["pf"][st], summary["esic"][st]])
        ws.cell(ws.max_row, 1).fill = PatternFill("solid", fgColor=COLOURS[st])
    ws.append(["Employees in wagesheet", summary["employees"], summary["employees"]])
    ws.append(["Total deducted (wagesheet)", summary["pf"]["deducted_total"], summary["esic"]["deducted_total"]])
    ws.append(["Total in ECR (employees listed)", summary["pf"]["deposited_total_for_listed"], summary["esic"]["deposited_total_for_listed"]])
    ws.append([])
    ws.append([f"Name tolerance: see matching.py. Amount tolerance: +-{AMOUNT_TOL:g} rupee."])
    for w in warnings:
        ws.append(["Warning: " + w])
    ws.column_dimensions["A"].width = 34

    ws = wb.create_sheet("Per employee")
    ws.append([h for _, h in RESULT_HEADERS])
    for c in ws[1]:
        c.font = bold
    for r in results:
        ws.append([r.get(k) for k, _ in RESULT_HEADERS])
        for key in ("pf_status", "esic_status"):
            idx = [k for k, _ in RESULT_HEADERS].index(key) + 1
            ws.cell(ws.max_row, idx).fill = PatternFill("solid", fgColor=COLOURS.get(r[key], "FFFFFF"))
    for i in range(1, len(RESULT_HEADERS) + 1):
        ws.column_dimensions[get_column_letter(i)].width = 16
    ws.column_dimensions["H"].width = 60
    ws.column_dimensions["N"].width = 60
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = ws.dimensions

    ws = wb.create_sheet("In ECR, not in wagesheet")
    ws.append(["ECR", "Identifier", "Name in ECR", "Amount"])
    for c in ws[1]:
        c.font = bold
    for x in extras:
        ws.append([x["kind"], x["identifier"], x["name"], x["amount"]])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
