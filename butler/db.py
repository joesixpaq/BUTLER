"""BUTLER registry — SQLite schema and helpers (P0: batches + sheets)."""
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
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init() -> None:
    with connect() as con:
        con.executescript(SCHEMA)
