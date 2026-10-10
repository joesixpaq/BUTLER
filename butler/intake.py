"""Scan-inbox intake: find finished scan runs and register them (idempotent).

A "run" is a folder produced by brother-pull-scan.py that contains manifest.json.
Runs without a manifest (legacy/pre-pipeline) are reported but not registered.
"""
import json
import pathlib
import time

from . import db
from .notify import send_telegram

SCAN_DIRS = [
    pathlib.Path.home() / "scans" / "direct",
    pathlib.Path.home() / "scans" / "incoming",
]


def find_runs(dirs=None):
    runs = []
    for d in (dirs or SCAN_DIRS):
        if not d.exists():
            continue
        for p in sorted(d.iterdir()):
            if p.is_dir() and (p.name.startswith("pull_") or d.name == "incoming"):
                runs.append(p)
    return runs


def ingest(notify: bool = True, verbose: bool = False):
    """Register new runs. Returns (new_batches, skipped_legacy).

    new_batches: list of (key, sheet_count)
    skipped_legacy: list of keys without manifest.json
    """
    db.init()
    new, skipped = [], []
    con = db.connect()
    try:
        for run in find_runs():
            key = f"{run.parent.name}/{run.name}"
            if con.execute("SELECT 1 FROM batches WHERE key=?", (key,)).fetchone():
                continue
            mf = run / "manifest.json"
            if not mf.exists():
                skipped.append(key)
                continue
            try:
                m = json.loads(mf.read_text())
            except (OSError, json.JSONDecodeError) as e:
                if verbose:
                    print(f"[intake] bad manifest {mf}: {e}")
                skipped.append(key)
                continue
            cur = con.execute(
                "INSERT INTO batches(key, ts, source, folder, status, note, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (key, m.get("scan_timestamp", ""), "scanner", str(run), "registered",
                 f"{len(m.get('sheets', []))} sheets · {m.get('stream_end', '')}", time.time()),
            )
            bid = cur.lastrowid
            for s in m.get("sheets", []):
                con.execute(
                    "INSERT INTO sheets(batch_id, sheet_no, front, back, meta_json) "
                    "VALUES (?,?,?,?,?)",
                    (bid, s.get("index"), s.get("front", ""), s.get("back", ""),
                     json.dumps({k: v for k, v in s.items() if k not in ("index", "front", "back")})),
                )
            con.commit()
            new.append((key, len(m.get("sheets", []))))
    finally:
        con.close()

    if new and notify:
        lines = "; ".join(f"{k} · {n} sheet(s)" for k, n in new)
        msg = f"BUTLER: {len(new)} new batch registered — {lines}"
        if skipped:
            msg += f"\n(legacy skipped: {', '.join(skipped)})"
        send_telegram(msg)
    return new, skipped
