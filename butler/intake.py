"""Scan-inbox intake: find finished scan runs and register them (idempotent).

A "run" is a folder produced by brother-pull-scan.py that contains manifest.json.
Runs without a manifest (legacy/pre-pipeline) are reported but not registered.

MANUAL SESSIONS (2026-10-11, button-driven one-by-one feeding): scan-watch
groups consecutive manual captures into sessions (`SESSIONS_DIR/<id>.json`,
`closed` = null while open). Intake registers a CLOSED session as ONE batch:

  key    `sessions/<id>`
  folder <scans root>          (sheet paths = `<run>/sheets/<file>`)
  sheets from all member runs, in run order, renumbered 1..N
  pdf    `scan_session_<id>.pdf` (merged from the member runs' scan PDFs)

Member runs are skipped by the per-run path (whether their session is open or
closed). A session still open is left alone — its runs wait; one open longer
than STALE_OPEN_HOURS is registered anyway as a safety net (scan-watch also
closes orphans on restart, so this is belt-and-braces).

Race guard (live bug 2026-10-12 23:38): scan-watch records a run's session
membership a moment AFTER the run's files land, so intake could see a finished
member run before the session file listed it and register it standalone
(observed: a 2-sheet run became its own batch while its session was still
open; the session batch then registered the same sheets again). Standalone
registration therefore waits out RUN_GRACE_SECONDS (manifest mtime) and
re-reads the session files freshly; and if a duplicate standalone batch did
slip through, registering the session retires it automatically (status
`trashed`, note "duplicate …", files untouched — they belong to the session
batch).

P2: no batch-level arrival ping — notifications are per mail, sent by the
analyzer after reading (spec: "each mail is special").
"""
import json
import pathlib
import time

from . import db, push

SCAN_DIRS = [
    pathlib.Path.home() / "scans" / "direct",
    pathlib.Path.home() / "scans" / "incoming",
]
SESSIONS_DIR = pathlib.Path.home() / "scans" / "sessions"
STALE_OPEN_HOURS = 12
RUN_GRACE_SECONDS = 60      # settle window before a standalone run registers


def find_runs(dirs=None):
    runs = []
    for d in (dirs or SCAN_DIRS):
        if not d.exists():
            continue
        for p in sorted(d.iterdir()):
            if p.is_dir() and (p.name.startswith("pull_") or d.name == "incoming"):
                if (p / "push.json").exists():
                    continue  # button-push batch folder — push.py owns it
                runs.append(p)
    return runs


def _load_sessions():
    """All session dicts + the set of run folder names they mention."""
    sessions, member_runs = [], set()
    if SESSIONS_DIR.exists():
        for p in sorted(SESSIONS_DIR.glob("*.json")):
            try:
                s = json.loads(p.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(s, dict) or not s.get("session_id"):
                continue
            runs = [r for r in (s.get("runs") or []) if isinstance(r, str)]
            member_runs.update(runs)
            sessions.append(s)
    return sessions, member_runs


def _session_age_hours(s):
    ts = s.get("started_ts")
    if isinstance(ts, (int, float)):
        return (time.time() - ts) / 3600.0
    started = s.get("started")
    if isinstance(started, str):
        try:
            import datetime
            return (time.time() - datetime.datetime.fromisoformat(started).timestamp()) / 3600.0
        except ValueError:
            pass
    return 0.0


def _merge_session_pdf(scans_root, runs, sid, verbose=False):
    """Best-effort: merge the member runs' scan PDFs into one session PDF."""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return None
    sources = []
    for run in runs:
        pdfs = sorted((scans_root / run).glob("scan_*.pdf"))
        if pdfs:
            sources.append(pdfs[-1])
    if not sources:
        return None
    out = scans_root / f"scan_session_{sid}.pdf"
    try:
        doc = fitz.open()
        for p in sources:
            with fitz.open(p) as d:
                doc.insert_pdf(d)
        doc.save(out)
        doc.close()
        return out
    except Exception as e:
        if verbose:
            print(f"[intake] session PDF merge failed for {sid}: {e}")
        return None


def _register_session(con, s, note_extra, verbose=False):
    """Register one session as a batch. Returns (key, sheet_count) or None."""
    sid = str(s.get("session_id"))
    runs = [r for r in (s.get("runs") or []) if isinstance(r, str)]
    scans_root = SCAN_DIRS[0]
    sheets_rows = []
    for run in runs:
        mf = scans_root / run / "manifest.json"
        if not mf.exists():
            continue
        try:
            m = json.loads(mf.read_text())
        except (OSError, json.JSONDecodeError) as e:
            if verbose:
                print(f"[intake] session {sid}: bad manifest {mf}: {e}")
            continue
        for sh in m.get("sheets", []):
            sheets_rows.append((run, sh))
    if not sheets_rows:
        return None
    ts = str(s.get("started") or "").replace("-", "").replace(":", "").replace("T", "_")
    note = f"{len(sheets_rows)} sheets · {len(runs)} run(s) · manual session{note_extra}"
    cur = con.execute(
        "INSERT INTO batches(key, ts, source, folder, status, note, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (f"sessions/{sid}", ts, "scanner", str(scans_root), "registered",
         note, time.time()),
    )
    bid = cur.lastrowid
    n = 0
    for run, sh in sheets_rows:
        n += 1
        con.execute(
            "INSERT INTO sheets(batch_id, sheet_no, front, back, meta_json) "
            "VALUES (?,?,?,?,?)",
            (bid, n, f"{run}/{sh.get('front', '')}", f"{run}/{sh.get('back', '')}",
             json.dumps({k: v for k, v in sh.items() if k not in ("index", "front", "back")})),
        )
    # Self-heal the membership race: a member run may already have been
    # registered standalone before the session file listed it — retire that
    # duplicate (registry-only; its files stay put for THIS session batch).
    for run in runs:
        for root in ("direct", "incoming"):
            dup = con.execute(
                "SELECT id FROM batches WHERE key=? "
                "AND COALESCE(status,'')!='trashed'",
                (f"{root}/{run}",)).fetchone()
            if dup:
                con.execute(
                    "UPDATE batches SET status='trashed', "
                    "note=COALESCE(note,'')||' · duplicate of sessions/'||?||"
                    "' (auto-retired)' WHERE id=?", (sid, dup["id"]))
    con.commit()
    _merge_session_pdf(scans_root, runs, sid, verbose=verbose)
    return (f"sessions/{sid}", n)


def ingest(notify: bool = True, verbose: bool = False):
    """Register new runs/sessions. Returns (new_batches, skipped_legacy).

    new_batches: list of (key, sheet_count)
    skipped_legacy: list of keys without manifest.json
    """
    db.init()
    new, skipped = [], []
    sessions, member_runs = _load_sessions()
    con = db.connect()
    try:
        # 0) physical-button pushes (device -> FTP -> incoming/*.pdf)
        new.extend(push.process_pending(con, verbose=verbose))

        # 1) sessions first — closed ones register; open ones hold their runs
        for s in sessions:
            key = f"sessions/{s.get('session_id')}"
            if con.execute("SELECT 1 FROM batches WHERE key=?", (key,)).fetchone():
                continue
            if s.get("closed"):
                note_extra = f" · closed:{s.get('closed_reason', '?')}"
            elif _session_age_hours(s) >= STALE_OPEN_HOURS:
                note_extra = " · stale-open (auto-registered)"
                if verbose:
                    print(f"[intake] session {s.get('session_id')}: stale open — registering anyway")
            else:
                continue  # still open — its runs wait (scan-watch closes it)
            reg = _register_session(con, s, note_extra, verbose=verbose)
            if reg:
                new.append(reg)

        # 2) standalone runs (not covered by any session)
        for run in find_runs():
            if run.name in member_runs:
                continue
            key = f"{run.parent.name}/{run.name}"
            if con.execute("SELECT 1 FROM batches WHERE key=?", (key,)).fetchone():
                continue
            mf = run / "manifest.json"
            if not mf.exists():
                skipped.append(key)
                continue
            # settle window + fresh membership re-read (see module docstring)
            if time.time() - mf.stat().st_mtime < RUN_GRACE_SECONDS:
                continue
            _, fresh_members = _load_sessions()
            if run.name in fresh_members:
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

    if new and verbose:
        print(f"[intake] registered: {new}")
    if skipped and verbose:
        print(f"[intake] legacy skipped: {skipped}")
    return new, skipped
