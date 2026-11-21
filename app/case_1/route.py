import csv
import io
import os
import re
from concurrent.futures import ThreadPoolExecutor
from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from app import db
from .agent import process_image

try:  # PDF support is optional: pip install pypdfium2
    import pypdfium2 as pdfium
except ImportError:
    pdfium = None

router = APIRouter(prefix="/api/cs1", tags=["cs1"])

pool = ThreadPoolExecutor(max_workers=2)
IMAGE_EXT = {".jpg", ".jpeg", ".png"}
PDF_EXT = {".pdf"}
UPLOAD_ROOT = os.path.join(db.ROOT, "data", "uploads")  # saved images live here


def expand_file(path: str, name: str):
    """Yield (record_name, image_bytes, error). A PDF yields one item per page."""
    ext = os.path.splitext(name)[1].lower()
    if ext in IMAGE_EXT:
        with open(path, "rb") as f:
            yield name, f.read(), None
    elif ext in PDF_EXT:
        if pdfium is None:
            yield name, None, "PDF support not installed (pip install pypdfium2)"
            return
        try:
            pdf = pdfium.PdfDocument(path)
            for i in range(len(pdf)):
                buf = io.BytesIO()
                pdf[i].render(scale=2).to_pil().save(buf, "PNG")
                yield f"{name}#page{i + 1}", buf.getvalue(), None
        except Exception as e:
            yield name, None, f"PDF could not be opened ({type(e).__name__})"
    else:
        yield name, None, f"Unsupported file type '{ext or 'none'}' (use JPG, PNG or PDF)"


def run_job(job_id: str, files: list[tuple[str, str]]):
    """files = [(saved_path_on_disk, original_name)]"""
    try:
        db.update_job(job_id, status="processing")
        items = []
        for path, name in files:
            for rec_name, data, err in expand_file(path, name):
                items.append((path, rec_name, data, err))
        db.update_job(job_id, total=len(items))

        for path, rec_name, data, err in items:
            if err:
                rec = {
                    "filename": rec_name,
                    "status": "UNREADABLE_FILE",
                    "flag_reason": err,
                }
            else:
                try:
                    rec = process_image(rec_name, data)
                except Exception as e:  # one bad image must never kill the batch
                    rec = {
                        "filename": rec_name,
                        "status": "PROCESSING_ERROR",
                        "flag_reason": f"Unexpected error: {type(e).__name__}: {str(e)[:150]}",
                    }
            rec["image_path"] = os.path.relpath(path, db.ROOT)
            db.add_result(job_id, rec)  # inserts the row and bumps progress
        db.update_job(job_id, status="completed")
    except Exception as e:
        db.update_job(
            job_id,
            status="failed",
            error=f"Unexpected error: {type(e).__name__}: {str(e)[:200]}",
        )


@router.post("/jobs")
async def create_job(files: list[UploadFile] = File(...)):
    file_name = ",".join([f.filename for f in files])

    job_id = db.create_job(source=file_name, kind="parcel")
    job_dir = os.path.join(UPLOAD_ROOT, job_id)
    os.makedirs(job_dir, exist_ok=True)

    saved = []
    for i, f in enumerate(files):
        data = await f.read()
        if not data:
            continue  # ignore empty files
        name = os.path.basename(f.filename or f"file_{i}")
        safe = re.sub(r"[^\w.\-]", "_", name)
        path = os.path.join(job_dir, f"{i:04d}_{safe}")
        with open(path, "wb") as out:
            out.write(data)  # image is saved before any processing starts
        saved.append((path, name))

    if not saved:
        db.update_job(
            job_id, status="failed", error="No files received, or all were empty."
        )
        raise HTTPException(400, "No files received, or all uploaded files were empty.")

    with db.conn() as c:
        c.execute(
            "UPDATE jobs SET source=? WHERE id=?",
            (f"{file_name}", job_id),
        )
    pool.submit(run_job, job_id, saved)
    return {"job_id": job_id, "files": len(saved)}


@router.get("/jobs")
def recent_jobs():
    return db.list_jobs(kind="parcel")


@router.get("/jobs/{job_id}")
def get_job(job_id: str):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return {**job, "results": db.get_results(job_id)}


@router.get("/results/{result_id}/image")
def get_image(result_id: int):
    rec = db.get_result(result_id)
    if not rec or not rec["image_path"]:
        raise HTTPException(404, "Image not found")
    full = os.path.realpath(os.path.join(db.ROOT, rec["image_path"]))
    if not full.startswith(os.path.realpath(UPLOAD_ROOT)) or not os.path.isfile(full):
        raise HTTPException(404, "Image file is missing on disk")
    return FileResponse(full)
