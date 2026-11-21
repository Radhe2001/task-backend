"""SQLite storage shared by Case 1 (parcel images), Case 2 (wagesheet vs ECR) and Case 3 (FC P&L monitor).

One `jobs` table serves all cases (told apart by `kind`):
  kind='parcel'  -> rows in `results`      (one per processed image)
  kind='wage'    -> rows in `wage_results` (one per employee) and `wage_extras` (ECR-only identifiers)
  kind='fc'      -> rows in `days` (one per FC per day, re-uploads overwrite), `anomalies`, `notifications`
"""

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "app.db")

# ---------- Case 1 (parcel) columns ----------
# columns exported to CSV
RESULT_COLUMNS = [
    "filename",
    "awb",
    "weight",
    "dimensions",
    "status",
    "flag_reason",
    "awb_source",
    "header_awb",
    "label_awb",
]
# columns written on insert (CSV columns + where the saved image lives)
INSERT_COLUMNS = RESULT_COLUMNS + ["image_path"]

# ---------- Case 2 (wage) columns ----------
TEXT, REAL, INT = "TEXT", "REAL", "INTEGER"
WAGE_SCHEMA = [
    ("row_no", INT),
    ("name", TEXT),
    ("uan", TEXT),
    ("ip_number", TEXT),
    ("agency", TEXT),
    ("state", TEXT),
    ("pf_wagesheet_amount", REAL),
    ("pf_ecr_name", TEXT),
    ("pf_ecr_amount", REAL),
    ("pf_difference", REAL),
    ("pf_status", TEXT),
    ("pf_reason", TEXT),
    ("esic_wagesheet_amount", REAL),
    ("esic_ecr_name", TEXT),
    ("esic_ecr_amount", REAL),
    ("esic_difference", REAL),
    ("esic_status", TEXT),
    ("esic_reason", TEXT),
]
WAGE_COLUMNS = [c for c, _ in WAGE_SCHEMA]

# ---------- Case 3 (FC P&L) columns ----------
# names match the keys produced by cs3.agent.compute()
_COSTS = ["manpower", "packaging", "power_fuel", "fc_rent", "equipment", "overheads"]
DAY_SCHEMA = (
    [("fc", TEXT), ("day", TEXT), ("revenue", REAL)]
    + [(k + suffix, REAL) for k in _COSTS for suffix in ("", "_pct")]
    + [
        ("cm1", REAL),
        ("cm1_pct", REAL),
        ("cm2", REAL),
        ("cm2_pct", REAL),
        ("colour", TEXT),
        ("insight", TEXT),
        ("unreported", TEXT),  # JSON list
    ]
)
DAY_COLUMNS = [c for c, _ in DAY_SCHEMA]
ANOMALY_COLUMNS = ["day", "fc", "line_item", "pct", "target_range", "status", "insight"]

# ---------- shared job columns ----------
JOB_COLUMNS = {"status", "stage", "total", "done", "error", "warnings", "summary"}
JSON_JOB_COLUMNS = ("warnings", "summary")


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def _columns(c, table: str) -> list[str]:
    return [r["name"] for r in c.execute(f"PRAGMA table_info({table})").fetchall()]


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with conn() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("""CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL DEFAULT 'parcel',
                source TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                stage TEXT DEFAULT '',
                total INTEGER DEFAULT 0,
                done INTEGER DEFAULT 0,
                error TEXT DEFAULT '',
                warnings TEXT DEFAULT '[]',
                summary TEXT DEFAULT '{}',
                created_at REAL NOT NULL
            )""")
        c.execute("""CREATE TABLE IF NOT EXISTS results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL REFERENCES jobs(id),
                filename TEXT, awb TEXT, weight TEXT, dimensions TEXT,
                status TEXT, flag_reason TEXT, awb_source TEXT,
                header_awb TEXT, label_awb TEXT,
                image_path TEXT DEFAULT '',
                created_at REAL NOT NULL
            )""")
        wage_cols = ", ".join(f"{n} {t}" for n, t in WAGE_SCHEMA)
        c.execute(f"""CREATE TABLE IF NOT EXISTS wage_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL REFERENCES jobs(id), {wage_cols})""")
        c.execute("""CREATE TABLE IF NOT EXISTS wage_extras (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL REFERENCES jobs(id),
                kind TEXT, identifier TEXT, name TEXT, amount REAL)""")

        # Case 3: one row per (fc, day); a re-upload of the same day replaces it
        day_cols = ", ".join(f"{n} {t}" for n, t in DAY_SCHEMA)
        c.execute(f"""CREATE TABLE IF NOT EXISTS days (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT, {day_cols}, UNIQUE (fc, day))""")
        c.execute("""CREATE TABLE IF NOT EXISTS anomalies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT, day TEXT, fc TEXT, line_item TEXT, pct REAL,
                target_range TEXT, status TEXT, insight TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day TEXT, fc TEXT, channel TEXT, recipient TEXT,
                status TEXT, detail TEXT, created_at REAL NOT NULL)""")

        # migrate jobs tables created by the Drive version (drive_url -> source)
        job_cols = _columns(c, "jobs")
        if "source" not in job_cols:
            if "drive_url" in job_cols:
                c.execute("ALTER TABLE jobs RENAME COLUMN drive_url TO source")
            else:
                c.execute("ALTER TABLE jobs ADD COLUMN source TEXT NOT NULL DEFAULT ''")
        # migrate jobs tables created before the merge (Case 1 only)
        job_cols = _columns(c, "jobs")
        for col, ddl in (
            ("kind", "TEXT NOT NULL DEFAULT 'parcel'"),
            ("stage", "TEXT DEFAULT ''"),
            ("warnings", "TEXT DEFAULT '[]'"),
            ("summary", "TEXT DEFAULT '{}'"),
        ):
            if col not in job_cols:
                c.execute(f"ALTER TABLE jobs ADD COLUMN {col} {ddl}")
        # migrate databases created before image_path existed
        if "image_path" not in _columns(c, "results"):
            c.execute("ALTER TABLE results ADD COLUMN image_path TEXT DEFAULT ''")

        c.execute("CREATE INDEX IF NOT EXISTS idx_results_job ON results(job_id)")
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_wage_results_job ON wage_results(job_id)"
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_wage_extras_job ON wage_extras(job_id)"
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_days_day ON days(day)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_anom_fc_day ON anomalies(fc, day)")
        # a restart kills background threads: mark their jobs so the UI can say so
        c.execute(
            "UPDATE jobs SET status='interrupted', "
            "error='Server restarted while this job was running. Upload the files again.' "
            "WHERE status IN ('queued','processing')"
        )


# ---------- jobs (shared by all cases) ----------
def create_job(source: str = "", kind: str = "parcel") -> str:
    job_id = uuid.uuid4().hex
    with conn() as c:
        c.execute(
            "INSERT INTO jobs (id, kind, source, status, created_at) VALUES (?,?,?,?,?)",
            (job_id, kind, source, "queued", time.time()),
        )
    return job_id


def update_job(job_id: str, **fields):
    fields = {
        k: (json.dumps(v) if k in JSON_JOB_COLUMNS else v)
        for k, v in fields.items()
        if k in JOB_COLUMNS
    }
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    with conn() as c:
        c.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*fields.values(), job_id))


def _job(row) -> dict:
    d = dict(row)
    d["warnings"] = json.loads(d.get("warnings") or "[]")
    d["summary"] = json.loads(d.get("summary") or "{}")
    return d


def get_job(job_id: str):
    with conn() as c:
        row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return _job(row) if row else None


def list_jobs(kind: str | None = None, with_results: bool = True) -> list[dict]:
    sql, args = "SELECT * FROM jobs", []
    if kind:
        sql += " WHERE kind=?"
        args.append(kind)
    sql += " ORDER BY created_at DESC"

    with conn() as c:
        jobs = [_job(r) for r in c.execute(sql, args).fetchall()]
        if not (with_results and jobs):
            return jobs

        # One query for every job's results, instead of one query per job.
        marks = ",".join("?" * len(jobs))
        result_rows = c.execute(
            f"SELECT * FROM results WHERE job_id IN ({marks}) ORDER BY id",
            [j["id"] for j in jobs],
        ).fetchall()

    by_job = defaultdict(list)
    for r in result_rows:
        d = dict(r)
        by_job[d["job_id"]].append(d)

    for j in jobs:
        j["results"] = by_job.get(j["id"], [])
        j["result_count"] = len(j["results"])
    return jobs


# ---------- Case 1: parcel results ----------
def add_result(job_id: str, rec: dict):
    """Insert one result and bump the job's done counter in a single transaction."""
    values = [
        str(rec.get(col)) if rec.get(col) is not None else "" for col in INSERT_COLUMNS
    ]
    with conn() as c:
        c.execute(
            f"INSERT INTO results (job_id, {', '.join(INSERT_COLUMNS)}, created_at) "
            f"VALUES (?, {', '.join('?' for _ in INSERT_COLUMNS)}, ?)",
            (job_id, *values, time.time()),
        )
        c.execute("UPDATE jobs SET done = done + 1 WHERE id=?", (job_id,))


def get_results(job_id: str) -> list[dict]:
    with conn() as c:
        rows = c.execute(
            f"SELECT id, {', '.join(RESULT_COLUMNS)}, image_path "
            "FROM results WHERE job_id=? ORDER BY id",
            (job_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_result(result_id: int):
    with conn() as c:
        row = c.execute("SELECT * FROM results WHERE id=?", (result_id,)).fetchone()
        return dict(row) if row else None


# ---------- Case 2: wagesheet vs ECR results ----------
def save_wage_results(job_id: str, results: list[dict], extras: list[dict]):
    with conn() as c:
        c.executemany(
            f"INSERT INTO wage_results (job_id, {', '.join(WAGE_COLUMNS)}) "
            f"VALUES (?, {', '.join('?' for _ in WAGE_COLUMNS)})",
            [(job_id, *[r.get(col) for col in WAGE_COLUMNS]) for r in results],
        )
        c.executemany(
            "INSERT INTO wage_extras (job_id, kind, identifier, name, amount) VALUES (?,?,?,?,?)",
            [
                (job_id, x["kind"], x["identifier"], x["name"], x["amount"])
                for x in extras
            ],
        )


def get_wage_results(job_id: str) -> list[dict]:
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM wage_results WHERE job_id=? ORDER BY id", (job_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_wage_extras(job_id: str) -> list[dict]:
    with conn() as c:
        rows = c.execute(
            "SELECT kind, identifier, name, amount FROM wage_extras WHERE job_id=? ORDER BY id",
            (job_id,),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------- Case 3: FC daily P&L ----------
def _day_row(row) -> dict:
    d = dict(row)
    d["unreported"] = json.loads(d.get("unreported") or "[]")
    return d


def save_days(job_id: str, days: list[dict], anomalies: list[dict]):
    """Upsert one row per (fc, day); anomalies of those (fc, day) pairs are replaced."""
    with conn() as c:
        for d in days:
            vals = [
                json.dumps(d.get(col, [])) if col == "unreported" else d.get(col)
                for col in DAY_COLUMNS
            ]
            c.execute(
                f"INSERT OR REPLACE INTO days (job_id, {', '.join(DAY_COLUMNS)}) "
                f"VALUES (?, {', '.join('?' for _ in DAY_COLUMNS)})",
                (job_id, *vals),
            )
            c.execute("DELETE FROM anomalies WHERE fc=? AND day=?", (d["fc"], d["day"]))
        c.executemany(
            f"INSERT INTO anomalies (job_id, {', '.join(ANOMALY_COLUMNS)}) "
            f"VALUES (?, {', '.join('?' for _ in ANOMALY_COLUMNS)})",
            [(job_id, *[a[col] for col in ANOMALY_COLUMNS]) for a in anomalies],
        )


def get_days(last_n: int | None = None) -> list[dict]:
    """All stored days (oldest first), or only the latest `last_n` distinct dates."""
    sql, args = "SELECT * FROM days", []
    if last_n:
        sql += " WHERE day IN (SELECT DISTINCT day FROM days ORDER BY day DESC LIMIT ?)"
        args.append(last_n)
    with conn() as c:
        return [
            _day_row(r) for r in c.execute(sql + " ORDER BY day, fc", args).fetchall()
        ]


def get_day(fc: str, day: str):
    with conn() as c:
        row = c.execute("SELECT * FROM days WHERE fc=? AND day=?", (fc, day)).fetchone()
        return _day_row(row) if row else None


def latest_day(fc: str | None = None):
    with conn() as c:
        row = c.execute(
            "SELECT MAX(day) AS d FROM days" + (" WHERE fc=?" if fc else ""),
            (fc,) if fc else (),
        ).fetchone()
        return row["d"]


def fc_trend(fc: str, day: str, n: int = 7) -> list[dict]:
    """The last n days up to and including `day`, oldest first (used for the colour strip in digests)."""
    with conn() as c:
        rows = c.execute(
            "SELECT day, colour, cm2_pct FROM days WHERE fc=? AND day<=? ORDER BY day DESC LIMIT ?",
            (fc, day, n),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]


def prior_revenue(fcs: list[str]) -> dict:
    """{fc: [(day, revenue), ...]} of everything stored so far: the system's memory of past days."""
    out = {fc: [] for fc in fcs}
    if not fcs:
        return out
    with conn() as c:
        rows = c.execute(
            f"SELECT fc, day, revenue FROM days WHERE fc IN ({','.join('?' for _ in fcs)}) ORDER BY day",
            fcs,
        ).fetchall()
    for r in rows:
        out[r["fc"]].append((r["day"], r["revenue"]))
    return out


def get_anomalies(
    fc: str | None = None, day: str | None = None, limit: int = 200
) -> list[dict]:
    """Newest day first; within a day, in the severity order the detector produced."""
    where, args = [], []
    if fc:
        where.append("fc=?")
        args.append(fc)
    if day:
        where.append("day=?")
        args.append(day)
    sql = "SELECT day, fc, line_item, pct, target_range, status, insight FROM anomalies"
    if where:
        sql += " WHERE " + " AND ".join(where)
    with conn() as c:
        rows = c.execute(
            sql + " ORDER BY day DESC, fc, id LIMIT ?", (*args, limit)
        ).fetchall()
        return [dict(r) for r in rows]


def log_notification(
    day: str, fc: str, channel: str, recipient: str, status: str, detail: str = ""
):
    with conn() as c:
        c.execute(
            "INSERT INTO notifications (day, fc, channel, recipient, status, detail, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (day, fc, channel, recipient, status, detail, time.time()),
        )


def get_notifications(limit: int = 100) -> list[dict]:
    with conn() as c:
        rows = c.execute(
            "SELECT day, fc, channel, recipient, status, detail, created_at "
            "FROM notifications ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
