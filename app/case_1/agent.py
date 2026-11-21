"""
Model-free parcel image processor.

No torch / transformers / Ollama / Tesseract. Only pip packages:
  opencv-python-headless, zxing-cpp, rapidocr-onnxruntime, pillow, numpy

Pipeline per image:
  1. Read the burned-in header (barcode, dimension, weight) with OCR + regex.
  2. Decode barcodes with zxing-cpp (full image, then per detected label blob).
  3. Detect parcels/labels with OpenCV (bright blobs) to set scene flags.
  4. Cross-check header AWB vs label AWB and emit one record, with a
     human-readable reason whenever something is missing or flagged.

All thresholds below are heuristics: tune them on your dataset.
"""

import io
import re

import cv2
import numpy as np
import zxingcpp
from PIL import Image
from rapidocr_onnxruntime import RapidOCR

_ocr = RapidOCR()

# ---------- tunables ----------
HEADER_BOX = (0.0, 0.0, 0.30, 0.14)  # x0, y0, x1, y1 as fractions of the image
WORK_SIZE = 1000  # longest side used for blob detection
THRESH_FLOOR = 50  # min brightness (0-255) to count as "parcel/label"
MIN_SIDE_FRAC = 0.04  # blob's short side must be >= 4% of image short side
MIN_AREA_FRAC = 0.005  # blob area must be >= 0.5% of image area
BORDER_PX = 3  # blob touching the frame edge => partial parcel
BLUR_MIN = 15.0  # Laplacian variance below this => unreadable (blurry)


# ---------- Tool 1: header OCR ----------
def read_header(img: Image.Image) -> dict:
    w, h = img.size
    x0, y0, x1, y1 = HEADER_BOX
    crop = img.crop((int(w * x0), int(h * y0), int(w * x1), int(h * y1)))
    crop = crop.resize((crop.width * 2, crop.height * 2))
    result, _ = _ocr(np.array(crop.convert("RGB")))
    text = "\n".join(r[1] for r in (result or []))
    out = {"_ocr_empty": not text.strip()}
    if m := re.search(r"Barcode\s*:\s*(\d{8,20})", text, re.I):
        out["awb"] = m.group(1)
    if m := re.search(r"L\s*(\d+)\s*X\s*H\s*(\d+)\s*X\s*W\s*(\d+)", text, re.I):
        out["dimensions"] = f"{int(m[1])}x{int(m[2])}x{int(m[3])}"
    if m := re.search(r"Weight\s*:\s*(\d+)", text, re.I):
        out["weight"] = int(m.group(1))
    if m := re.search(r"Location\s*:\s*(\S+)", text, re.I):
        out["location"] = m.group(1)
    return out


# ---------- Tool 2: scene analysis with OpenCV (no model) ----------
def find_blobs(img: Image.Image) -> dict:
    """Find large bright regions (parcels / labels). Returns blobs in
    original-image coordinates as (x, y, w, h) plus a partial-parcel flag."""
    gray = np.array(img.convert("L"))
    H, W = gray.shape
    scale = WORK_SIZE / max(H, W)
    small = cv2.resize(gray, (max(1, int(W * scale)), max(1, int(H * scale))))
    h, w = small.shape

    # blank out the header text so it is not mistaken for a parcel
    x0, y0, x1, y1 = HEADER_BOX
    small[: int(y1 * h) + 2, : int(x1 * w) + 2] = 0

    blur = cv2.GaussianBlur(small, (5, 5), 0)
    otsu, _ = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    thr = max(float(otsu), THRESH_FLOOR)
    mask = (blur > thr).astype(np.uint8) * 255
    mask = cv2.morphologyEx(
        mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (15, 15))
    )

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    min_side = MIN_SIDE_FRAC * min(h, w)
    min_area = MIN_AREA_FRAC * h * w

    blobs, partial = [], False
    for c in contours:
        x, y, bw, bh = cv2.boundingRect(c)
        if min(bw, bh) < min_side or cv2.contourArea(c) < min_area:
            continue  # thin streaks, noise, specks
        if (
            x <= BORDER_PX
            or y <= BORDER_PX
            or x + bw >= w - BORDER_PX
            or y + bh >= h - BORDER_PX
        ):
            partial = True
        blobs.append((int(x / scale), int(y / scale), int(bw / scale), int(bh / scale)))

    return {"blobs": blobs, "partial": partial}


def sharpness(img: Image.Image, blob) -> float:
    gray = np.array(img.convert("L"))
    x, y, bw, bh = blob
    crop = gray[y : y + bh, x : x + bw]
    if crop.size == 0:
        return 0.0
    return float(cv2.Laplacian(crop, cv2.CV_64F).var())


# ---------- Tool 3: barcode on label ----------
def _decode(gray: np.ndarray) -> list[str]:
    return [b.text for b in zxingcpp.read_barcodes(gray)]


def read_barcodes(img: Image.Image, blobs: list) -> list[str]:
    gray = np.array(img.convert("L"))
    codes = _decode(gray)
    if codes:
        return codes
    # retry on each label blob, enhanced and upscaled (barcodes are small)
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    for x, y, bw, bh in blobs:
        pad = int(0.05 * max(bw, bh))
        crop = gray[max(0, y - pad) : y + bh + pad, max(0, x - pad) : x + bw + pad]
        if crop.size == 0:
            continue
        for scale in (1.0, 2.0):
            c = (
                crop
                if scale == 1.0
                else cv2.resize(
                    crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC
                )
            )
            codes = _decode(clahe.apply(c))
            if codes:
                return codes
    return []


# ---------- Agent: orchestrate + decide ----------
def process_image(filename: str, data: bytes) -> dict:
    rec = {
        "filename": filename,
        "awb": "",
        "weight": "",
        "dimensions": "",
        "status": "OK",
        "flag_reason": "",
        "awb_source": "",
        "header_awb": "",
        "label_awb": "",
    }
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        img = img.convert("RGB")
    except Exception as e:
        rec.update(
            status="CORRUPT_IMAGE",
            flag_reason=f"File could not be decoded as an image ({type(e).__name__})",
        )
        return rec

    try:
        header = read_header(img)
        scene = find_blobs(img)
        codes = read_barcodes(img, scene["blobs"])
    except Exception as e:
        rec.update(
            status="PROCESSING_ERROR",
            flag_reason=f"Processing crashed: {type(e).__name__}: {str(e)[:150]}",
        )
        return rec

    rec["weight"] = header.get("weight", "")
    rec["dimensions"] = header.get("dimensions", "")
    h_awb = header.get("awb")
    l_awb = next((c for c in codes if c.isdigit() and len(c) >= 8), None)
    rec["header_awb"] = h_awb or ""
    rec["label_awb"] = l_awb or ""

    reasons: list[str] = []

    # --- scene flags (heuristic) ---
    blobs = scene["blobs"]
    n = len(blobs)
    if n == 0:
        rec["status"] = "NO_PARCEL"
        reasons.append("No parcel-sized bright region found in the frame")
    elif n > 1:
        rec["status"] = "MULTIPLE_PARCELS"
        reasons.append(f"{n} parcel-like regions found in the frame")
    elif scene["partial"]:
        rec["status"] = "PARTIAL_PARCEL"
        reasons.append("Parcel touches the frame edge, so it is likely cut off")

    # --- AWB resolution (a disagreement is never guessed away) ---
    if h_awb and l_awb and h_awb != l_awb:
        rec.update(status="LOW_CONFIDENCE", awb="")
        reasons.append(
            f"AWB not trusted: header says {h_awb} but label barcode says {l_awb}"
        )
    elif h_awb or l_awb:
        rec["awb"] = h_awb or l_awb
        rec["awb_source"] = (
            "both" if (h_awb and l_awb) else ("header" if h_awb else "label")
        )
    else:
        causes = []
        if header.get("_ocr_empty"):
            causes.append("header OCR returned no text (header missing or unreadable)")
        else:
            causes.append("header text found but no 'Barcode:' number in it")
        if not blobs:
            causes.append("no label region to decode a barcode from")
        else:
            causes.append("label barcode could not be decoded")
        blurry = False
        if blobs:
            biggest = max(blobs, key=lambda b: b[2] * b[3])
            s = sharpness(img, biggest)
            blurry = s < BLUR_MIN
            causes.append(
                f"label region is blurry (sharpness {s:.0f}, minimum {BLUR_MIN:.0f})"
                if blurry
                else f"label region sharpness is OK ({s:.0f}), so the label may be blocked, "
                "damaged, or the barcode too small"
            )
        if rec["status"] == "OK":
            rec["status"] = "LABEL_UNREADABLE" if blurry else "AWB_NOT_FOUND"
        reasons.append("AWB missing: " + "; ".join(causes))

    # --- other missing fields (informational, does not change the status) ---
    if rec["weight"] == "":
        reasons.append("Weight not found in header")
    if rec["dimensions"] == "":
        reasons.append("Dimensions not found in header")

    rec["flag_reason"] = " | ".join(reasons)
    return rec
