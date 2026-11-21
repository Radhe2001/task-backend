import os
import re
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response

from app import db
from .agent import build_workbook, read_wagesheet, summarize, verify
from .parsers import parse_ecr_pdfs

UPLOAD_ROOT = os.path.join(db.ROOT, "data", "uploads")
STATIC = os.path.join(db.ROOT, "static")
pool = ThreadPoolExecutor(max_workers=2)  # jobs run in the background
SHEET_EXT = {".xlsx", ".xlsm", ".csv"}

router = APIRouter(prefix="/api/cs2", tags=["cs2"])

# Router-level startup hooks are ignored by FastAPI, so create the tables here.
# init_db() is idempotent (CREATE TABLE IF NOT EXISTS), so it is safe if main.py also calls it.
os.makedirs(UPLOAD_ROOT, exist_ok=True)
db.init_db()


def run_job(job_id: str, sheet_path: str, pf_paths: list[str], esic_paths: list[str]):
    try:
        db.update_job(
            job_id, status="processing", stage="Reading wagesheet", done=0, total=100
        )
        warnings: list[str] = []
        rows, w = read_wagesheet(sheet_path)
        warnings += w
        db.update_job(job_id, done=5)

        def make_progress(label, start, span):
            def cb(page, pages, fname):
                db.update_job(
                    job_id,
                    stage=f"Reading {label} ECR: page {page}/{pages} of {fname}",
                    done=start + int(span * page / max(pages, 1)),
                )

            return cb

        pf = esic = None
        if pf_paths:
            pf, w = parse_ecr_pdfs(
                pf_paths, "PF", make_progress("PF", 5, 45 if esic_paths else 90)
            )
            warnings += w
        if esic_paths:
            start = 50 if pf_paths else 5
            esic, w = parse_ecr_pdfs(
                esic_paths, "ESIC", make_progress("ESIC", start, 45 if pf_paths else 90)
            )
            warnings += w

        db.update_job(job_id, stage="Matching employees", done=95)
        results, extras = verify(rows, pf, esic)
        db.save_wage_results(job_id, results, extras)
        db.update_job(
            job_id,
            status="completed",
            stage="Done",
            done=100,
            summary=summarize(results),
            warnings=warnings,
        )
    except Exception as e:  # clear message for the user, never a silent crash
        db.update_job(
            job_id, status="failed", error=f"{type(e).__name__}: {str(e)[:300]}"
        )


async def _save(files: list[UploadFile], job_dir: str, prefix: str, kind: str):
    """Validate and save uploads. kind: 'sheet' or 'pdf'. Returns saved paths."""
    saved = []
    for i, f in enumerate(files or []):
        if not f.filename:
            continue  # empty file input in the browser form
        data = await f.read()
        name = os.path.basename(f.filename)
        ext = os.path.splitext(name)[1].lower()
        if not data:
            raise HTTPException(400, f"'{name}' is empty.")
        if kind == "pdf" and (
            ext != ".pdf" or not data.lstrip()[:5].startswith(b"%PDF")
        ):
            raise HTTPException(400, f"'{name}' is not a valid PDF.")
        if kind == "sheet":
            if ext not in SHEET_EXT:
                raise HTTPException(
                    400, f"'{name}': the wagesheet must be .xlsx or .csv."
                )
            if ext != ".csv" and not data.startswith(b"PK"):
                raise HTTPException(400, f"'{name}' is not a valid Excel file.")
        safe = re.sub(r"[^\w.\-]", "_", name)
        path = os.path.join(job_dir, f"{prefix}_{i:03d}_{safe}")
        with open(path, "wb") as out:
            out.write(data)
        saved.append(path)
    return saved


@router.post("/jobs")
async def create_job(
    wagesheet: UploadFile = File(...),
    pf_ecr: list[UploadFile] = File(default=[]),
    esic_ecr: list[UploadFile] = File(default=[]),
):
    job_id = db.create_job(source=wagesheet.filename, kind="wage")
    job_dir = os.path.join(UPLOAD_ROOT, job_id)
    os.makedirs(job_dir, exist_ok=True)
    try:
        sheet = await _save([wagesheet], job_dir, "wagesheet", "sheet")
        pf = await _save(pf_ecr, job_dir, "pf", "pdf")
        esic = await _save(esic_ecr, job_dir, "esic", "pdf")
        if not sheet:
            raise HTTPException(400, "Please choose the wagesheet file.")
        if not pf and not esic:
            raise HTTPException(400, "Upload at least one ECR PDF (PF or ESIC).")
    except HTTPException as e:
        db.update_job(job_id, status="failed", error=e.detail)
        raise
    with db.conn() as c:
        c.execute(
            "UPDATE jobs SET source=? WHERE id=?",
            (
                f"{wagesheet.filename}, ",
                job_id,
            ),
        )
    pool.submit(run_job, job_id, sheet[0], pf, esic)
    return {"job_id": job_id}


@router.get("/jobs")
def recent_jobs():
    return db.list_jobs(kind="wage")


@router.get("/jobs/{job_id}")
def get_job(job_id: str):
    job = db.get_job(job_id)
    if not job or job["kind"] != "wage":
        raise HTTPException(404, "Job not found")
    return {
        **job,
        "results": db.get_wage_results(job_id),
        "extras": db.get_wage_extras(job_id),
    }


@router.get("/jobs/{job_id}/xlsx")
def get_xlsx(job_id: str):
    job = db.get_job(job_id)
    if not job or job["kind"] != "wage":
        raise HTTPException(404, "Job not found")
    if job["status"] != "completed":
        raise HTTPException(409, f"Job is {job['status']}; results are not ready.")
    data = build_workbook(
        db.get_wage_results(job_id),
        db.get_wage_extras(job_id),
        job["summary"],
        job["warnings"],
    )
    return Response(
        data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f"attachment; filename=wage_vs_ecr_{job_id[:8]}.xlsx"
        },
    )
