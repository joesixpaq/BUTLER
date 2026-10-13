"""BUTLER registry — SQLite schema and helpers (P0: batches+sheets · P2: analyses+facts)."""
import pathlib
import sqlite3

from . import ROOT

DB_PATH = ROOT / "var" / "butler.db"

SHEET_STATUSES = ["inbox", "analyzing", "review", "action", "waiting", "filed", "noise"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches(
  id         INTEGER PRIMARY KEY,
  key        TEXT UNIQUE,           -- "<scandir>/<run>" e.g. "direct/pull_20261010_213755"
  ts         TEXT,                  -- scan timestamp from manifest
  source     TEXT,                  -- scanner | ftp | email (later)
  folder     TEXT,                  -- absolute run folder
  status     TEXT DEFAULT 'registered',
  board_status TEXT,                -- user-set letter column (NULL = derived from sheets)
  note       TEXT,
  notified   INTEGER DEFAULT 0,
  created_at REAL
);
CREATE TABLE IF NOT EXISTS sheets(
  id         INTEGER PRIMARY KEY,
  batch_id   INTEGER REFERENCES batches(id),
  sheet_no   INTEGER,               -- 1..N within batch (front/back pair per sheet)
  front      TEXT,                  -- path relative to batch folder
  back       TEXT,
  status     TEXT DEFAULT 'inbox',  -- board column (letter-facing status lives on the batch)
  excluded       INTEGER DEFAULT 0, -- scan removed from the letter (files kept on disk)
  front_excluded INTEGER DEFAULT 0, -- single side removed (e.g. blank back)
  back_excluded  INTEGER DEFAULT 0,
  meta_json  TEXT
);
CREATE INDEX IF NOT EXISTS idx_sheets_batch ON sheets(batch_id);
CREATE INDEX IF NOT EXISTS idx_sheets_status ON sheets(status);
CREATE TABLE IF NOT EXISTS analyses(
  id        INTEGER PRIMARY KEY,
  sheet_id  INTEGER UNIQUE REFERENCES sheets(id),
  ts        REAL,
  model     TEXT,
  status    TEXT,                   -- ok | error
  error     TEXT,
  json      TEXT,                   -- cleaned analysis JSON (documents, facts)
  raw       TEXT,                   -- raw model reply (QA/debug)
  attempts  INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS facts(
  id         INTEGER PRIMARY KEY,
  sheet_id   INTEGER REFERENCES sheets(id),
  doc_idx    INTEGER DEFAULT 0,
  kind       TEXT,
  value      TEXT,
  citation   TEXT,
  confidence REAL
);
CREATE INDEX IF NOT EXISTS idx_facts_sheet ON facts(sheet_id);
CREATE TABLE IF NOT EXISTS tasks(
  id         INTEGER PRIMARY KEY,
  sheet_id   INTEGER REFERENCES sheets(id),
  title      TEXT,
  kind       TEXT DEFAULT 'task',   -- task | deadline (auto, from analysis facts)
  due        TEXT,                  -- ISO YYYY-MM-DD (NULL = no date)
  status     TEXT DEFAULT 'open',   -- open | done | cancelled
  source     TEXT DEFAULT 'user',   -- user | analysis
  created_at REAL,
  done_at    REAL
);
CREATE INDEX IF NOT EXISTS idx_tasks_sheet ON tasks(sheet_id);
CREATE INDEX IF NOT EXISTS idx_tasks_open ON tasks(status, due);
CREATE TABLE IF NOT EXISTS reminders(
  id          INTEGER PRIMARY KEY,
  task_id     INTEGER REFERENCES tasks(id),
  offset_days INTEGER,              -- 14|7|3|1 ... -1 = overdue notice
  sent_at     REAL,
  UNIQUE(task_id, offset_days)
);
CREATE TABLE IF NOT EXISTS payments(
  id         INTEGER PRIMARY KEY,
  sheet_id   INTEGER UNIQUE REFERENCES sheets(id),
  ts         REAL,
  payload    TEXT,                  -- raw Swiss QR payload (SPC ...)
  data_json  TEXT,                  -- parsed payment data (prepared only — the user pays)
  check_json TEXT,                  -- cross-check vs the analysis facts
  status     TEXT DEFAULT 'prepared',  -- prepared | paid
  status_ts  REAL
);
CREATE TABLE IF NOT EXISTS faces(
  id         INTEGER PRIMARY KEY,
  sheet_id   INTEGER REFERENCES sheets(id),
  batch_id   INTEGER REFERENCES batches(id),
  side       TEXT,                  -- front | back
  h0         TEXT,                  -- rotation-tolerant dHash, 256 bits hex each:
  h90        TEXT,                  -- the side image rotated by k degrees
  h180       TEXT,                  -- before hashing (0/90/180/270)
  h270       TEXT,
  blank      INTEGER DEFAULT 0,     -- near-empty page: no identity value
  ts         REAL,
  UNIQUE(sheet_id, side)
);
CREATE INDEX IF NOT EXISTS idx_faces_batch ON faces(batch_id);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init() -> None:
    with connect() as con:
        con.executescript(SCHEMA)
        _migrate(con)


def _migrate(con) -> None:
    """Additive migrations for registries created before the batch-view rework
    (2026-10-12): letter column on batches; scan/side removal flags on sheets."""
    bcols = {r["name"] for r in con.execute("PRAGMA table_info(batches)")}
    if "board_status" not in bcols:
        con.execute("ALTER TABLE batches ADD COLUMN board_status TEXT")
    if "released" not in bcols:
        # user pressed "analyze now" — bypasses the hold + schedule gates
        con.execute("ALTER TABLE batches ADD COLUMN released INTEGER DEFAULT 0")
    scols = {r["name"] for r in con.execute("PRAGMA table_info(sheets)")}
    for c in ("excluded", "front_excluded", "back_excluded"):
        if c not in scols:
            con.execute(f"ALTER TABLE sheets ADD COLUMN {c} INTEGER DEFAULT 0")


def get_sheet(sid: int):
    con = connect()
    try:
        return con.execute(
            "SELECT s.*, b.folder, b.key AS batch_key, b.ts AS batch_ts "
            "FROM sheets s JOIN batches b ON b.id=s.batch_id WHERE s.id=?", (sid,)
        ).fetchone()
    finally:
        con.close()
