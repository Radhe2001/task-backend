"""Name matching with an explicit, documented tolerance.

Goal: catch genuine mismatches (a different person's name on an identifier)
without crying wolf on trivial variations.

TOLERATED (reported as a match, with a note saying which rule fired):
  1. Case, punctuation, dots, extra spaces          "R.K. Sharma" == "R K SHARMA"
  2. Titles                                         "MR RAHUL SINGH" == "RAHUL SINGH"
  3. Word order                                     "KUMAR RAHUL" == "RAHUL KUMAR"
  4. Word spacing                                   "SUNIL KUMAR" == "SUNILKUMAR"
  5. Initials                                       "R KUMAR" ~ "RAVI KUMAR"
  6. Mohammad abbreviations                         "MD ALAM" ~ "MOHAMMAD ALAM"
  7. One dropped word (>= 2 words must still match) "RAM KUMAR" ~ "RAM KUMAR SHARMA"
  8. ONE small typo in ONE word: same first letter, word length >= 4,
     edit distance <= 1 (<= 2 for words of 8+ letters).

NOT tolerated (reported as a name mismatch):
  - A different first name or surname ("AMIT KUMAR" vs "SUNIL KUMAR"; first
    letters differ, so "SUNIL" vs "ANIL" is never treated as a typo)
  - Two or more typos, or a single-word name vs a longer name
  - A match resting only on initials (nothing exact or typo-level to anchor on)
"""

import re
from difflib import SequenceMatcher

TITLES = {"MR", "MRS", "MS", "MISS", "SHRI", "SMT", "SRI", "SHREE", "DR", "LATE"}
ALIASES = {
    "MD": "MOHAMMAD",
    "MOHD": "MOHAMMAD",
    "MOHAMMED": "MOHAMMAD",
    "MUHAMMAD": "MOHAMMAD",
    "MOHAMAD": "MOHAMMAD",
}
_RANK = {"exact": 0, "typo": 1, "initial": 2}


def tokens(name) -> list[str]:
    s = re.sub(r"[^A-Z]+", " ", str(name or "").upper())
    return [ALIASES.get(t, t) for t in s.split() if t not in TITLES]


def _lev(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _tok_match(a: str, b: str):
    if a == b:
        return "exact"
    if len(a) == 1 or len(b) == 1:
        return "initial" if a[0] == b[0] else None
    if len(a) >= 4 and len(b) >= 4 and a[0] == b[0]:
        limit = 2 if min(len(a), len(b)) >= 8 else 1
        if _lev(a, b) <= limit:
            return "typo"
    return None


def compare_names(a, b) -> tuple[bool, float, str]:
    """Returns (is_match, similarity 0-1, note)."""
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return False, 0.0, "name missing in one of the documents"
    sa, sb = sorted(ta), sorted(tb)
    score = SequenceMatcher(None, " ".join(sa), " ".join(sb)).ratio()
    if ta == tb:
        return True, 1.0, "identical"
    if sa == sb:
        return True, 1.0, "same words in a different order"
    if "".join(sa) == "".join(sb):
        return True, 1.0, "same letters, different word spacing"

    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if len(long_) - len(short) > 1:
        return False, score, "names differ"
    if len(short) == 1 and len(long_) > 1:
        return False, score, "names differ (one-word name vs longer name)"

    free, kinds = list(long_), []
    for t in sorted(short, key=len, reverse=True):
        best = None
        for i, u in enumerate(free):
            k = _tok_match(t, u)
            if k and (best is None or _RANK[k] < _RANK[best[1]]):
                best = (i, k)
        if best is None:
            return False, score, "names differ"
        kinds.append(best[1])
        free.pop(best[0])

    if kinds.count("typo") > 1:
        return False, score, "names differ (more than one spelling difference)"
    if not any(k in ("exact", "typo") for k in kinds):
        return False, score, "names differ (only initials in common)"
    notes = []
    if "typo" in kinds:
        notes.append("minor spelling difference")
    if "initial" in kinds:
        notes.append("initial vs full name")
    if free:
        notes.append("one word missing in one document")
    return True, score, "tolerated: " + ", ".join(notes)
