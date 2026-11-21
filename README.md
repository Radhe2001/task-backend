# Backend — Ops Automation API

A FastAPI server that powers three operations automation tools.  
All data is stored in a local SQLite database (`data/app.db`); no external database is required.

---

## Prerequisites

| Tool | Version |
|---|---|
| Python | 3.11 or later |
| pip | bundled with Python |

---

## Setup

```bash
# 1. Enter the backend directory
cd backend

# 2. Create a virtual environment (recommended, only needed once)
python -m venv .venv

# 3. Activate it
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
# Windows CMD
.\.venv\Scripts\activate.bat
# macOS / Linux
source .venv/bin/activate

# 4. Install all dependencies
pip install -r requirements.txt
```

---

## Start

```bash
python run.py
```

The API starts at **http://localhost:8000** with auto-reload enabled.  
Interactive docs are available at **http://localhost:8000/docs**.

### Environment variables (all optional)

| Variable | Default | Purpose |
|---|---|---|
| `HOST` | `0.0.0.0` | Bind address |
| `PORT` | `8000` | Bind port |

---

## Project layout

```
backend/
├── run.py               # Entry point – starts uvicorn
├── requirements.txt
├── app/
│   ├── main.py          # FastAPI app, CORS, router mounts, startup hook
│   ├── config.py        # HOST / PORT from environment
│   ├── db.py            # SQLite schema + all query helpers (shared by all cases)
│   ├── case_1/
│   │   ├── agent.py     # OCR + barcode + blob detection pipeline
│   │   └── route.py     # /api/cs1/* endpoints
│   ├── case_2/
│   │   ├── agent.py     # Wagesheet reader, verifier, Excel exporter
│   │   ├── matching.py  # Fuzzy employee-name comparison
│   │   ├── parsers.py   # PF / ESIC ECR PDF parser
│   │   └── route.py     # /api/cs2/* endpoints
│   └── case_3/
│       ├── agent.py     # P&L computation, anomaly detection, insight generation
│       ├── pipeline.py  # Orchestrator: ingest → compute → store → notify
│       └── route.py     # /api/cs3/* endpoints
└── data/
    ├── app.db           # SQLite database (created on first run)
    ├── uploads/         # Uploaded files, one sub-folder per job
```

---

## Case 1 — Parcel Image Extractor

**Endpoint prefix:** `/api/cs1`

### What it does

Processes one or more parcel images (JPG, PNG) or PDF files. For each image it:

1. **Reads the header** (top-left 30 × 14% of the image) with OCR to extract AWB barcode, dimensions, weight, and location.
2. **Detects parcels / labels** in the frame using OpenCV blob detection.
3. **Decodes barcodes** from the full image, then retries on each detected label blob with contrast enhancement and upscaling.
4. **Decides a status** by cross-checking the two AWB sources and the scene layout.

### Status values

| Status | Meaning |
|---|---|
| `OK` | Parcel found, AWB extracted, everything matches |
| `PARTIAL_PARCEL` | A parcel is visible but it is cut off at the edge of the frame |
| `MULTIPLE_PARCELS` | More than one parcel-sized region found in the frame |
| `NO_PARCEL` | No parcel-sized bright region found at all |
| `LOW_CONFIDENCE` | Header AWB and label barcode disagree — AWB is left blank intentionally |
| `AWB_NOT_FOUND` | No AWB from either source (image is sharp; label may be blocked or damaged) |
| `LABEL_UNREADABLE` | No AWB found and the label region is blurry |
| `CORRUPT_IMAGE` | File could not be decoded as an image |
| `UNREADABLE_FILE` | Unsupported file type or PDF could not be opened |
| `PROCESSING_ERROR` | Unexpected crash during processing (one bad image never kills the whole batch) |

### Workflow

```
POST /api/cs1/jobs                   ← upload files → returns job_id
GET  /api/cs1/jobs                   ← list recent jobs (with results)
GET  /api/cs1/jobs/{id}              ← job status + per-image results
GET  /api/cs1/results/{id}/image     ← serve the original image
```

Processing runs in a background thread pool. Poll `GET /jobs/{id}` until `status` is `completed` or `failed`.

---

## Case 2 — Wagesheet vs ECR Compliance

**Endpoint prefix:** `/api/cs2`

### What it does

Cross-checks employee PF and ESIC deductions in a wagesheet against official ECR (Electronic Challan cum Return) PDFs.

**Inputs:**
- **Wagesheet** — `.xlsx` or `.csv` with columns: Name, UAN, IP Number, PF amount, ESIC amount
- **PF ECR PDFs** _(optional)_ — one or more PF ECR PDFs
- **ESIC ECR PDFs** _(optional)_ — one or more ESIC ECR PDFs

At least one ECR PDF (PF or ESIC) is required.

### Verdict per employee (PF and ESIC independently)

| Status | Meaning |
|---|---|
| `MATCHED` | Identifier found in ECR, name close enough, amount within ±₹1 tolerance |
| `NAME_MISMATCH` | Identifier found, but the name in the ECR differs from the wagesheet |
| `AMOUNT_MISMATCH` | Identifier + name match, but the deduction amount differs |
| `MISSING` | Identifier not found in the ECR even though an amount was deducted |
| `NOT_APPLICABLE` | No deduction made and no matching identifier expected (e.g. salary above ESIC ceiling) |
| `REVIEW` | Found in ECR but the amount could not be reliably read from the PDF |
| `NOT_CHECKED` | That ECR was not uploaded, so this side was not verified |

The exported Excel workbook has three sheets:
- **Summary** — counts per status and totals for PF and ESIC
- **Per employee** — full detail with colour coding
- **In ECR, not in wagesheet** — identifiers that appear in the ECR but are not on the wagesheet

### Workflow

```
POST /api/cs2/jobs              ← upload wagesheet + ECR PDFs → returns job_id
GET  /api/cs2/jobs              ← list recent jobs
GET  /api/cs2/jobs/{id}         ← job status + per-employee results
GET  /api/cs2/jobs/{id}/xlsx    ← download the Excel report (only when completed)
```

---

## Case 3 — FC Daily P&L Monitor

**Endpoint prefix:** `/api/cs3`

### What it does

Ingests daily cost data for one or more Fulfilment Centres, computes contribution margins, detects anomalies against target ranges, generates plain-English insights, and optionally sends digests.

**Input file** — `.xlsx` or `.csv` with these columns (column names are flexible):

| Column | Required | Notes |
|---|---|---|
| Day / Date | ✅ | Day number (1, 2, 3…) or a calendar date |
| Revenue | ✅ | |
| Manpower Cost | ✅ | |
| Packaging Cost | ✅ | |
| Power & Fuel | ✅ | |
| FC Rent | ✅ | |
| Equipment Rentals | ✅ | |
| Overheads | ✅ | |
| FC / Centre | _(optional)_ | Required only when the file covers multiple FCs |

A separate **Targets file** (`.xlsx` or `.csv`) can be uploaded to override the default % ranges. If not provided, the system looks for a target tab inside the data workbook, then falls back to built-in defaults.

### Computed metrics

```
CM1 = Revenue − Manpower − Packaging
CM2 = CM1 − Rent − Power&Fuel − Equipment − Overheads
```

### CM2 colour coding

| Colour | CM2 % range | Meaning |
|---|---|---|
| 🔴 Red | < 12% | Below the red threshold — urgent cost review needed |
| 🟡 Yellow | 12–14.9% or 30–33% | Watch zone — margins at risk |
| 🟢 Green | 15–30% | Within target |
| 🔵 Blue | > 33% | Suspiciously high — possible under-reported costs |

### Anomaly detection

Each line item is flagged `ABOVE MAX` or `BELOW MIN` when its percentage of revenue falls outside its target range. CM1% and CM2% have their own limits (CM1: 65–70% ±3 pp; CM2: 15–30% ±3 pp). Flags are sorted by financial impact (rupees above the ceiling, highest first).

### API endpoints

```
POST /api/cs3/jobs            ← upload data file (+ optional targets file, FC name, notify flag)
GET  /api/cs3/jobs            ← list recent upload jobs
GET  /api/cs3/jobs/{id}       ← job status

```

### Re-uploading data

Uploading data for a day that is already in the database **replaces** that day's rows. Historical days are unaffected. This makes it safe to correct and re-upload a single day's file.


## Database

SQLite (`data/app.db`) is created automatically on first startup. All three cases share the same file:

| Table | Used by |
|---|---|
| `jobs` | All cases — tracks upload status and progress |
| `results` | Case 1 — one row per processed image |
| `wage_results` | Case 2 — one row per employee |
| `wage_extras` | Case 2 — identifiers in ECR but not in the wagesheet |
| `days` | Case 3 — one row per (FC, day); re-uploads replace the row |
| `anomalies` | Case 3 — one row per flagged line item per day |
| `notifications` | Case 3 — delivery log for digests |

The database is safe to delete; it will be recreated on the next server start (all uploaded data and results will be lost).