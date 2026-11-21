import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .case_1 import route as case_1_router
from .case_2 import route as case_2_router
from .case_3 import route as case_3_router
from . import db

try:  # PDF support is optional: pip install pypdfium2
    import pypdfium2 as pdfium
except ImportError:
    pdfium = None

app = FastAPI(title="Parcel image extractor")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(case_1_router.router)
app.include_router(case_2_router.router)
app.include_router(case_3_router.router)

UPLOAD_ROOT = os.path.join(db.ROOT, "data", "uploads")  # saved images live here


@app.on_event("startup")
def startup():
    os.makedirs(UPLOAD_ROOT, exist_ok=True)
    db.init_db()
