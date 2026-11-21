"""Parse the PF ECR and ESIC ECR PDFs into {identifier: record} maps.

Text-based parsing (pdfplumber): every line that contains an identifier is a
member row. Layout assumptions (override with env vars if your PDFs differ):

  PF ECR   row: [sr] UAN  NAME  gross  epf_wages  eps_wages  edli_wages  EE_share  eps  er ...
           -> amount = numeric column PF_EE_INDEX   (default 4 = EE share)
  ESIC     row: [sr] IP_NO NAME days  wages  IP_contribution  reason  last_working_day
           -> amount = numeric column ESIC_AMT_INDEX (default 2 = IP contribution)

Numbers are the numeric tokens that follow the name on the same line (dates
like 01/05/2026 are not numeric tokens). A sanity check compares the picked
amount with rate x wages across the file and warns when the layout looks wrong.
Scanned (image-only) PDFs yield no rows and raise a clear error.
"""

import os
import re

import pdfplumber

PF_EE_INDEX = int(os.getenv("PF_EE_INDEX", "4"))
PF_WAGE_INDEX = int(os.getenv("PF_WAGE_INDEX", "1"))
ESIC_AMT_INDEX = int(os.getenv("ESIC_AMT_INDEX", "2"))
ESIC_WAGE_INDEX = int(os.getenv("ESIC_WAGE_INDEX", "1"))

_ID = {"PF": re.compile(r"^\d{12}$"), "ESIC": re.compile(r"^(\d{10}|\d{17})$")}
_NUM = re.compile(r"^\d+(\.\d+)?$")
_WORD = re.compile(r"^[A-Za-z][A-Za-z.'\-]*$")


def parse_line(line: str, kind: str):
    toks = line.split()
    for i, t in enumerate(toks):
        if _ID[kind].match(t):
            j = i + 1
            name = []
            while j < len(toks) and _WORD.match(toks[j]):
                name.append(toks[j])
                j += 1
            nums = [float(x.replace(",", "")) for x in toks[j:] if _NUM.match(x.replace(",", ""))]
            return t, " ".join(name), nums
    return None


def parse_ecr_pdfs(paths: list[str], kind: str, progress=None):
    """Returns (records, warnings). records[id] = {name, amount, count, wage}."""
    amt_i, wage_i, rate = (
        (PF_EE_INDEX, PF_WAGE_INDEX, 0.12) if kind == "PF" else (ESIC_AMT_INDEX, ESIC_WAGE_INDEX, 0.0075)
    )
    recs, warnings = {}, []
    unreadable_amount, checked, plausible = 0, 0, 0
    for path in paths:
        try:
            pdf = pdfplumber.open(path)
        except Exception as e:
            raise ValueError(f"{os.path.basename(path)} could not be opened as a PDF ({type(e).__name__})")
        with pdf:
            n = len(pdf.pages)
            for pno, page in enumerate(pdf.pages, 1):
                text = page.extract_text() or ""
                page.flush_cache()
                for line in text.splitlines():
                    row = parse_line(line, kind)
                    if not row:
                        continue
                    ident, name, nums = row
                    amount = nums[amt_i] if len(nums) > amt_i else None
                    wage = nums[wage_i] if len(nums) > wage_i else None
                    if amount is None:
                        unreadable_amount += 1
                    elif wage:
                        checked += 1
                        plausible += abs(amount - rate * wage) <= 1.01
                    if ident in recs:  # same member listed twice: add up, and say so
                        r = recs[ident]
                        r["count"] += 1
                        r["amount"] = (r["amount"] or 0) + (amount or 0)
                    else:
                        recs[ident] = {"name": name, "amount": amount, "count": 1}
                if progress:
                    progress(pno, n, os.path.basename(path))
    if not recs:
        raise ValueError(
            f"No {kind} member rows found. Check that this is the {kind} ECR and that the PDF has "
            "selectable text (scanned image PDFs are not supported)."
        )
    if unreadable_amount:
        warnings.append(f"{kind} ECR: {unreadable_amount} row(s) had too few numeric columns to read the amount.")
    if checked >= 5 and plausible / checked < 0.5:
        warnings.append(
            f"{kind} ECR: only {plausible}/{checked} rows have amount ~ {rate:.2%} of wages, so the column layout "
            f"may differ from the assumed one. Adjust {'PF_EE_INDEX' if kind == 'PF' else 'ESIC_AMT_INDEX'}."
        )
    dups = sum(1 for r in recs.values() if r["count"] > 1)
    if dups:
        warnings.append(f"{kind} ECR: {dups} identifier(s) appear more than once; amounts were added up.")
    return recs, warnings
