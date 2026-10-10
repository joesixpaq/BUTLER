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
  status     TEXT DEFAULT 'inbox',  -- board column
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
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init() -> None:
    with connect() as con:
        con.executescript(SCHEMA)


def set_status(sid: int, status: str) -> None:
    with connect() as con:
        con.execute("UPDATE sheets SET status=? WHERE id=?", (status, sid))


def get_sheet(sid: int):
    con = connect()
    try:
        return con.execute(
            "SELECT s.*, b.folder, b.key AS batch_key, b.ts AS batch_ts "
            "FROM sheets s JOIN batches b ON b.id=s.batch_id WHERE s.id=?", (sid,)
        ).fetchone()
    finally:
        con.close()
