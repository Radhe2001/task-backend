import csv
import io
import os
import re
from concurrent.futures import ThreadPoolExecutor
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from openpyxl import Workbook
from . import agent, notifier, pipeline
from app import db

router = APIRouter(prefix="/api/cs3", tags=["cs3"])


UPLOAD_ROOT = os.path.join(db.ROOT, "data", "uploads")
STATIC = os.path.join(db.ROOT, "static")
pool = ThreadPoolExecutor(max_workers=2)
OK_EXT = {".csv", ".xlsx", ".xlsm"}

PNL_HEADERS = (
    ["Date", "FC", "Revenue"]
    + [h for li in agent.LINE_ITEMS for h in (li, li + " %")]
    + ["CM1", "CM1 %", "CM2", "CM2 %", "Colour"]
)
ANOM_HEADERS = [
    "Date",
    "FC",
    "Line Item",
    "% of Revenue",
    "Target Range",
    "Status",
    "Insight",
]


# @router.on_event("startup")
# def startup():
#     os.makedirs(UPLOAD_ROOT, exist_ok=True)
#     db.init_db()


def run_job(job_id: str, path: str, fc_name: str, targets_path, notify: bool):
    def progress(stage, pct):
        db.update_job(job_id, stage=stage, done=pct)

    try:
        db.update_job(job_id, status="processing", stage="Starting", done=0)
        summary = pipeline.ingest(path, fc_name, targets_path, job_id, notify, progress)
        db.update_job(
            job_id, status="completed", stage="Done", done=100, summary=summary
        )
    except Exception as e:  # clear message for the user, never a silent crash
        db.update_job(
            job_id, status="failed", error=f"{type(e).__name__}: {str(e)[:300]}"
        )


async def _save(f: UploadFile, job_dir: str, prefix: str):
    data = await f.read()
    name = os.path.basename(f.filename or "")
    ext = os.path.splitext(name)[1].lower()
    if not data:
        raise HTTPException(400, f"'{name or 'file'}' is empty.")
    if ext not in OK_EXT:
        raise HTTPException(400, f"'{name}': use .xlsx or .csv.")
    if ext != ".csv" and not data.startswith(b"PK"):
        raise HTTPException(400, f"'{name}' is not a valid Excel file.")
    safe = re.sub(r"[^\w.\-]", "_", name)
    path = os.path.join(job_dir, f"{prefix}_{safe}")
    with open(path, "wb") as out:
        out.write(data)
    return path


@router.post("/jobs")
async def create_job(
    data_file: UploadFile = File(...),
    targets_file: UploadFile | None = File(default=None),
    fc_name: str = Form(""),
    notify: str = Form("true"),
):
    file_name = data_file.filename or "" + fc_name
    job_id = db.create_job(kind="fc_data", source=file_name)
    job_dir = os.path.join(UPLOAD_ROOT, job_id)
    os.makedirs(job_dir, exist_ok=True)
    try:
        path = await _save(data_file, job_dir, "data")
        tpath = (
            await _save(targets_file, job_dir, "targets")
            if targets_file and targets_file.filename
            else None
        )
    except HTTPException as e:
        db.update_job(job_id, status="failed", error=e.detail)
        raise
    pool.submit(run_job, job_id, path, fc_name, tpath, notify.lower() == "true")
    return {"job_id": job_id}


@router.get("/jobs")
def recent_jobs():
    return db.list_jobs(kind="fc_data")


@router.get("/jobs/{job_id}")
def get_job(job_id: str):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


@router.get("/dashboard")
def dashboard(days: int = 14):
    rows = db.get_days(days)
    dates = sorted({r["day"] for r in rows})
    fcs = sorted({r["fc"] for r in rows})
    flags = {}
    for a in db.get_anomalies(limit=100000):
        flags[(a["fc"], a["day"])] = flags.get((a["fc"], a["day"]), 0) + 1
    cells = {fc: {} for fc in fcs}
    for r in rows:
        cells[r["fc"]][r["day"]] = {
            "colour": r["colour"],
            "cm2_pct": round(r["cm2_pct"], 1),
            "cm1_pct": round(r["cm1_pct"], 1),
            "flags": flags.get((r["fc"], r["day"]), 0),
        }
    return {"dates": dates, "fcs": fcs, "cells": cells}


@router.get("/day")
def day_detail(fc: str, day: str):
    d = db.get_day(fc, day)
    if not d:
        raise HTTPException(404, "No data for that FC and day")
    return {"day": d, "anomalies": db.get_anomalies(fc, day)}


@router.get("/anomalies")
def anomalies(fc: str | None = None, day: str | None = None, limit: int = 200):
    return db.get_anomalies(fc, day, limit)


@router.get("/notifications")
def notifications():
    return db.get_notifications()


@router.post("/digests/send")
def send(day: str | None = None, fc: str | None = None):
    if db.latest_day() is None:
        raise HTTPException(409, "No data yet. Upload a file first.")
    return notifier.send_digests(day, [fc] if fc else None)


def _csv(headers, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(headers)
    w.writerows(rows)
    return buf.getvalue()


def _pnl_rows():
    out = []
    for d in db.get_days():
        row = [d["day"], d["fc"], round(d["revenue"], 2)]
        for li in agent.LINE_ITEMS:
            row += [round(d[agent.KEYS[li]], 2), round(d[agent.KEYS[li] + "_pct"], 2)]
        out.append(
            row
            + [
                round(d["cm1"], 2),
                round(d["cm1_pct"], 2),
                round(d["cm2"], 2),
                round(d["cm2_pct"], 2),
                d["colour"],
            ]
        )
    return out


def _anom_rows():
    return [
        [
            a["day"],
            a["fc"],
            a["line_item"],
            a["pct"],
            a["target_range"],
            a["status"],
            a["insight"],
        ]
        for a in db.get_anomalies(limit=100000)
    ]


@router.get("/pnl/csv")
def pnl_csv():
    return Response(
        _csv(PNL_HEADERS, _pnl_rows()),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=fc_daily_pnl.csv"},
    )


@router.get("/anomalies/csv")
def anomalies_csv():
    return Response(
        _csv(ANOM_HEADERS, _anom_rows()),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=fc_anomalies.csv"},
    )


@router.get("/export/xlsx")
def export_xlsx():
    wb = Workbook()
    ws = wb.active
    ws.title = "Daily P&L"
    ws.append(PNL_HEADERS)
    for r in _pnl_rows():
        ws.append(r)
    ws = wb.create_sheet("Anomalies")
    ws.append(ANOM_HEADERS)
    for r in _anom_rows():
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return Response(
        buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=fc_pnl_report.xlsx"},
    )
