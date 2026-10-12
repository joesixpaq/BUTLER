"""BUTLER board — Kanban browser for scanned mail (v2: analysis + scan sessions).

Run:  python3 -m butler.web      (systemd: butler-board.service, port 9500)

Columns = sheet statuses. Cards show the analyzed mail (title, chips, key
facts); detail view = front/back images + full analysis (cited facts).

Scanner panel: the board shows the ADS-1000W status (from scan-watch's state
file) and drives the manual one-by-one feeding flow — "Scan sheet" / "Finish
batch" write a request file that scan-watch (the single device controller)
picks up and executes. A finished session registers as one batch via intake.

Trash: a batch can be archived from its sheet view (`POST /api/batch/<id>/trash`)
— files move to `~/scans/archive/`, the batch is marked `trashed` and drops off
the board. Nothing is ever hard-deleted.
"""
import json
import os
import pathlib
import shutil
import time
import uuid

from datetime import date

from flask import (Flask, abort, jsonify, redirect, render_template,
                   request, send_file, url_for)

from . import ROOT, db, qrpay, tasks

app = Flask(__name__, template_folder=str(ROOT / "templates"),
            static_folder=str(ROOT / "static"))


def _scans_root() -> pathlib.Path:
    return pathlib.Path(os.environ.get("BUTLER_SCANS_DIR",
                                       str(pathlib.Path.home() / "scans")))


def _sw_cfg() -> dict:
    """scan-watch paths (same defaults as scan-watch.py; scanwatch.json wins)."""
    cfg = {
        "state_file": str(pathlib.Path.home() / "lan-plan" / "scan-watch.state.json"),
        "request_file": str(pathlib.Path.home() / "lan-plan" / "scan-request.json"),
        "sessions_dir": str(_scans_root() / "sessions"),
    }
    try:
        raw = json.loads((pathlib.Path.home() / ".config/butler/scanwatch.json").read_text())
        for k in cfg:
            if raw.get(k):
                cfg[k] = raw[k]
    except Exception:
        pass
    cfg["state_file"] = os.environ.get("BUTLER_SW_STATE", cfg["state_file"])
    cfg["request_file"] = os.environ.get("BUTLER_SW_REQUEST", cfg["request_file"])
    cfg["sessions_dir"] = os.environ.get("BUTLER_SW_SESSIONS", cfg["sessions_dir"])
    return cfg


def _sw_state() -> dict:
    try:
        return json.loads(pathlib.Path(_sw_cfg()["state_file"]).read_text())
    except Exception:
        return {}


def _sheet_row(sid):
    con = db.connect()
    try:
        return con.execute(
            "SELECT s.*, b.folder, b.key AS batch_key, b.ts AS batch_ts, b.note AS batch_note, "
            "b.status AS batch_status, "
            "a.status AS astatus, a.error AS aerror, a.json AS ajson "
            "FROM sheets s JOIN batches b ON b.id = s.batch_id "
            "LEFT JOIN analyses a ON a.sheet_id = s.id WHERE s.id = ?", (sid,)
        ).fetchone()
    finally:
        con.close()


def _abs_path(folder, rel):
    root = pathlib.Path(folder).resolve()
    p = (root / rel).resolve()
    if root not in p.parents and p != root:
        abort(403)
    return p


def _batch_pdf(batch_key, folder):
    """Batch PDF: sessions -> merged session PDF; runs -> newest in-folder PDF."""
    if batch_key and batch_key.startswith("sessions/"):
        sid = batch_key.split("/", 1)[1]
        p = pathlib.Path(folder) / f"scan_session_{sid}.pdf"
        return p if p.exists() else None
    pdfs = sorted(pathlib.Path(folder).glob("scan_*.pdf"))
    return pdfs[-1] if pdfs else None


PRI_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


def _batch_board_status(b, kept):
    """Letter column: the user's move wins; otherwise derive it from its scans.

    Derived: fresh -> inbox; anything still being read -> analyzing (with
    progress on the card); all analyzed -> review; all noise -> noise.
    A letter whose scans were all removed shows under noise (nothing left).
    """
    if b.get("board_status"):
        return b["board_status"]
    if not kept:
        return "noise"
    sts = [s.get("status") for s in kept]
    if any(st == "analyzing" for st in sts):
        return "analyzing"
    if any(st == "inbox" for st in sts):
        done = any(st in ("review", "action", "waiting", "filed") for st in sts)
        return "analyzing" if done else "inbox"
    if all(st == "noise" for st in sts):
        return "noise"
    return "review"


@app.get("/")
def board():
    """Board of LETTERS — one card per batch (a batch = the sheets of one letter).

    Every user-facing action is letter-wide (drag = move the whole letter);
    the only per-scan actions are removing/restoring a scan or a side
    (e.g. a blank back) — those live on the letter page.
    """
    con = db.connect()
    try:
        brows = [dict(b) for b in con.execute(
            "SELECT * FROM batches WHERE COALESCE(status, '') != 'trashed' "
            "ORDER BY id DESC")]
        srows = [dict(s) for s in con.execute(
            "SELECT s.*, a.status AS astatus, a.error AS aerror, a.json AS ajson "
            "FROM sheets s LEFT JOIN analyses a ON a.sheet_id = s.id "
            "ORDER BY s.batch_id, s.sheet_no, s.id")]
        tmap = {}
        for r in con.execute(
                "SELECT sheet_id, MIN(CASE WHEN due IS NOT NULL THEN due END) AS due "
                "FROM tasks WHERE status='open' GROUP BY sheet_id"):
            tmap[r["sheet_id"]] = r["due"]
        pmap = {r["sheet_id"]: r for r in con.execute(
            "SELECT sheet_id, status, data_json FROM payments")}
    finally:
        con.close()

    by_batch = {}
    for r in srows:
        by_batch.setdefault(r["batch_id"], []).append(r)

    today = date.today()
    cards = []
    scans_kept = 0
    for b in brows:
        kept = sorted((s for s in by_batch.get(b["id"], []) if not s.get("excluded")),
                      key=lambda s: (s["sheet_no"] or 0, s["id"]))
        scans_kept += len(kept)
        docs = []
        for s in kept:
            if s.get("ajson"):
                try:
                    docs += (json.loads(s["ajson"]) or {}).get("documents") or []
                except (TypeError, json.JSONDecodeError):
                    pass
        real = [d for d in docs if (d.get("disposition") or "") != "noise"] or docs
        prim = real[0] if real else None
        pri = min((d.get("priority") or "P3" for d in real),
                  key=lambda p: PRI_ORDER.get(p, 3)) if real else "P3"
        cover = None
        for s in kept:
            if s.get("front") and not s.get("front_excluded"):
                cover = {"sid": s["id"], "side": "front"}
                break
            if s.get("back") and not s.get("back_excluded"):
                cover = {"sid": s["id"], "side": "back"}
                break
        due_iso = None
        for s in kept:
            d = tmap.get(s["id"])
            if d and (due_iso is None or d < due_iso):
                due_iso = d
        pay = None
        for s in kept:
            p = pmap.get(s["id"])
            if p:
                try:
                    pd = json.loads(p["data_json"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    pd = {}
                pay = {"status": p["status"],
                       "amount": f"{pd.get('amount') or ''} {pd.get('currency') or 'CHF'}".strip()}
                break
        card = {
            "b": b, "prim": prim, "pri": pri, "cover": cover,
            "n_scans": len(kept),
            "analyzed_n": sum(1 for s in kept if s.get("astatus") == "ok"),
            "errors_n": sum(1 for s in kept if s.get("astatus") == "error"),
            "status": _batch_board_status(b, kept),
            "due_fmt": None, "due_cls": "", "pay": pay,
        }
        if due_iso:
            dd0 = date.fromisoformat(due_iso)
            dd = (dd0 - today).days
            card["due_fmt"] = tasks.fmt_date(dd0)
            card["due_cls"] = "due-past" if dd < 0 else ("due-soon" if dd <= 7 else "")
        cards.append(card)

    cols = {st: [] for st in db.SHEET_STATUSES}
    for c in cards:
        cols.setdefault(c["status"], []).append(c)
    next_due, n_open = tasks.open_summary()
    return render_template("board.html", cols=cols, statuses=db.SHEET_STATUSES,
                           letters=len(brows), scans=scans_kept,
                           tasks_open=n_open,
                           tasks_next=tasks.fmt_date(date.fromisoformat(next_due))
                           if next_due else None)


@app.get("/sheet/<int:sid>")
def sheet_view(sid):
    """Scan zoom view — a part of its letter. Letter-wide status only."""
    s = _sheet_row(sid)
    if not s:
        abort(404)
    analysis = None
    if s["ajson"]:
        try:
            analysis = json.loads(s["ajson"])
        except (TypeError, json.JSONDecodeError):
            analysis = None
    pdf = _batch_pdf(s["batch_key"], s["folder"])
    con = db.connect()
    try:
        facts = [dict(f) for f in con.execute(
            "SELECT * FROM facts WHERE sheet_id=? ORDER BY doc_idx, id", (sid,))]
        b = con.execute("SELECT * FROM batches WHERE id=?", (s["batch_id"],)).fetchone()
        sibs = [dict(r) for r in con.execute(
            "SELECT id, status, excluded FROM sheets WHERE batch_id=?", (s["batch_id"],))]
    finally:
        con.close()
    kept = [x for x in sibs if not x["excluded"]]
    letter_status = _batch_board_status(dict(b), kept) if b else s["status"]
    return render_template("sheet.html", s=s, statuses=db.SHEET_STATUSES, pdf=pdf,
                           analysis=analysis, facts=facts,
                           letter_status=letter_status,
                           tasks=tasks.sheet_task_rows(sid),
                           payment=qrpay.payment_for_sheet(sid))


@app.get("/batch/<int:bid>")
def batch_view(bid):
    """The LETTER page — all scans of one letter plus its letter-wide actions."""
    con = db.connect()
    try:
        b = con.execute("SELECT * FROM batches WHERE id=?", (bid,)).fetchone()
        if not b:
            abort(404)
        sheets = [dict(s) for s in con.execute(
            "SELECT s.*, a.status AS astatus, a.error AS aerror, a.json AS ajson "
            "FROM sheets s LEFT JOIN analyses a ON a.sheet_id = s.id "
            "WHERE s.batch_id=? ORDER BY s.sheet_no, s.id", (bid,))]
    finally:
        con.close()
    kept = [s for s in sheets if not s.get("excluded")]
    removed = [s for s in sheets if s.get("excluded")]
    for s in kept:
        s["docs"] = []
        if s.get("ajson"):
            try:
                s["docs"] = (json.loads(s["ajson"]) or {}).get("documents") or []
            except (TypeError, json.JSONDecodeError):
                pass
    status = _batch_board_status(dict(b), kept)
    pdf = _batch_pdf(b["key"], b["folder"])
    payment = None
    for s in kept:
        payment = qrpay.payment_for_sheet(s["id"])
        if payment:
            break
    trows = []
    for s in kept:
        for t in tasks.sheet_task_rows(s["id"]):
            t["sheet_no"] = s["sheet_no"]
            trows.append(t)
    return render_template("batch.html", b=b, sheets=kept, removed=removed,
                           status=status, statuses=db.SHEET_STATUSES, pdf=pdf,
                           payment=payment, tasks=trows,
                           trashed=(b["status"] == "trashed"))


@app.get("/img/<int:sid>/<side>")
def img(sid, side):
    if side not in ("front", "back"):
        abort(400)
    s = _sheet_row(sid)
    if not s or not s[side]:
        abort(404)
    p = _abs_path(s["folder"], s[side])
    if not p.exists():
        abort(404)
    return send_file(p, mimetype="image/jpeg")


@app.get("/pdf/<int:bid>")
def pdf(bid):
    con = db.connect()
    try:
        b = con.execute("SELECT * FROM batches WHERE id = ?", (bid,)).fetchone()
    finally:
        con.close()
    if not b:
        abort(404)
    p = _batch_pdf(b["key"], b["folder"])
    if not p:
        abort(404)
    return send_file(p, mimetype="application/pdf", as_attachment=False)


@app.post("/api/batch/<int:bid>/status")
def set_batch_status(bid):
    """Move the WHOLE letter (all its scans) to another column."""
    st = (request.get_json(silent=True) or {}).get("status") or request.form.get("status")
    if st not in db.SHEET_STATUSES:
        abort(400, description=f"status must be one of {db.SHEET_STATUSES}")
    con = db.connect()
    try:
        if not con.execute("SELECT id FROM batches WHERE id=?", (bid,)).fetchone():
            abort(404)
        con.execute("UPDATE batches SET board_status=? WHERE id=?", (st, bid))
        con.execute("UPDATE sheets SET status=? WHERE batch_id=?", (st, bid))
        con.commit()
    finally:
        con.close()
    if request.form:
        return redirect(url_for("batch_view", bid=bid))
    return jsonify(ok=True, status=st)


@app.post("/api/sheet/<int:sid>/exclude")
def exclude_scan(sid):
    """Remove/restore one scan (scope=scan) or one side (front|back) from a letter.

    Files are never touched — this only hides the scan/side from the letter
    view and from future analysis passes; value=0 restores.
    """
    body = request.get_json(silent=True) or {}
    scope = body.get("scope") or request.form.get("scope") or "scan"
    value = body.get("value")
    if value is None:
        value = request.form.get("value", "1")
    value = 1 if str(value).lower() in ("1", "true", "yes", "on") else 0
    if scope not in ("scan", "front", "back"):
        abort(400, description="scope must be scan|front|back")
    col = {"scan": "excluded", "front": "front_excluded", "back": "back_excluded"}[scope]
    con = db.connect()
    try:
        row = con.execute("SELECT batch_id FROM sheets WHERE id=?", (sid,)).fetchone()
        if not row:
            abort(404)
        con.execute(f"UPDATE sheets SET {col}=? WHERE id=?", (value, sid))
        con.commit()
        bid = row["batch_id"]
    finally:
        con.close()
    if request.form:
        return redirect(url_for("batch_view", bid=bid))
    return jsonify(ok=True, scope=scope, value=value)


@app.post("/api/sheet/<int:sid>/requeue")
def requeue(sid):
    """Drop the analysis and put the sheet back in the queue (re-analyze)."""
    con = db.connect()
    try:
        con.execute("DELETE FROM analyses WHERE sheet_id = ?", (sid,))
        con.execute("DELETE FROM facts WHERE sheet_id = ?", (sid,))
        con.execute("UPDATE sheets SET status = 'inbox' WHERE id = ?", (sid,))
        con.commit()
    finally:
        con.close()
    if request.form:
        return redirect(url_for("sheet_view", sid=sid))
    return jsonify(ok=True)


# --------------------------------------------------------------------------- #
# P3 — tasks, reminders (.ics), payment prep
# --------------------------------------------------------------------------- #

@app.post("/api/sheet/<int:sid>/task")
def add_task(sid):
    body = request.get_json(silent=True) or {}
    title = (body.get("title") or request.form.get("title") or "").strip()
    due_raw = (body.get("due") or request.form.get("due") or "").strip()
    if not title:
        abort(400, description="title required")
    due = None
    if due_raw:
        due = tasks.parse_date(due_raw)
        if not due:
            abort(400, description=f"unparseable due date: {due_raw!r}")
    con = db.connect()
    try:
        if not con.execute("SELECT id FROM sheets WHERE id=?", (sid,)).fetchone():
            abort(404)
    finally:
        con.close()
    tid = tasks.add_task(sid, title, due=due)
    if request.form:
        return redirect(url_for("sheet_view", sid=sid))
    return jsonify(ok=True, id=tid)


@app.post("/api/task/<int:tid>/status")
def task_status(tid):
    st = (request.get_json(silent=True) or {}).get("status") or request.form.get("status")
    if st not in tasks.TASK_STATUSES:
        abort(400, description=f"status must be one of {tasks.TASK_STATUSES}")
    con = db.connect()
    try:
        row = con.execute("SELECT s.batch_id FROM tasks t JOIN sheets s "
                          "ON s.id = t.sheet_id WHERE t.id=?", (tid,)).fetchone()
    finally:
        con.close()
    if not row:
        abort(404)
    tasks.set_task_status(tid, st)
    if request.form:
        # mutations return to the letter that owns the task (letter = unit)
        return redirect(url_for("batch_view", bid=row["batch_id"]))
    return jsonify(ok=True, status=st)


@app.post("/api/payment/<int:pid>/status")
def payment_status(pid):
    st = (request.get_json(silent=True) or {}).get("status") or request.form.get("status")
    if st not in ("prepared", "paid"):
        abort(400, description="status must be prepared|paid")
    con = db.connect()
    try:
        row = con.execute("SELECT s.batch_id FROM payments p JOIN sheets s "
                          "ON s.id = p.sheet_id WHERE p.id=?", (pid,)).fetchone()
    finally:
        con.close()
    if not row:
        abort(404)
    qrpay.set_payment_status(pid, st)
    if request.form:
        # mutations return to the letter that owns the payment (letter = unit)
        return redirect(url_for("batch_view", bid=row["batch_id"]))
    return jsonify(ok=True, status=st)


@app.get("/ics/<int:tid>")
def task_ics(tid):
    """Download one task as an .ics all-day event (deadline calendar entry)."""
    con = db.connect()
    try:
        t = con.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        title = tasks.mail_title(con, t["sheet_id"]) if t else None
    finally:
        con.close()
    if not t or not t["due"]:
        abort(404)
    resp = app.response_class(tasks.make_ics(t, mail_title=title),
                              mimetype="text/calendar")
    resp.headers["Content-Disposition"] = f"attachment; filename=butler-task-{tid}.ics"
    return resp


# --------------------------------------------------------------------------- #
# scanner panel — status + manual session requests
# --------------------------------------------------------------------------- #

@app.get("/api/scanner")
def api_scanner():
    st = _sw_state()
    return jsonify(ok=True, online=(st.get("mode") == "online"),
                   mode=st.get("mode"), updated=st.get("updated"),
                   session=st.get("session"), last_session=st.get("last_session"),
                   last_request=st.get("last_request"))


@app.post("/api/scan")
def api_scan():
    body = request.get_json(silent=True) or {}
    action = body.get("action")
    if action not in ("scan", "finish"):
        abort(400, description="action must be 'scan' or 'finish'")
    if action == "scan":
        st = _sw_state()
        if st.get("mode") == "suspended":
            return jsonify(ok=False, error="scanner suspended — power-cycle it first"), 409
        if st.get("mode") != "online":
            return jsonify(ok=False, error="scanner offline"), 409
    req = {"action": action, "id": uuid.uuid4().hex[:12], "ts": time.time()}
    p = pathlib.Path(_sw_cfg()["request_file"])
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.parent / (p.name + ".tmp")
    tmp.write_text(json.dumps(req))
    os.replace(tmp, p)
    return jsonify(ok=True, request=req)


# --------------------------------------------------------------------------- #
# trash — archive a batch (never delete)
# --------------------------------------------------------------------------- #

@app.post("/api/batch/<int:bid>/trash")
def trash_batch(bid):
    con = db.connect()
    moved = []
    try:
        b = con.execute("SELECT * FROM batches WHERE id = ?", (bid,)).fetchone()
        if not b:
            abort(404)
        archive = _scans_root() / "archive"
        archive.mkdir(parents=True, exist_ok=True)
        key = b["key"] or ""

        def _move(p):
            if not p.exists():
                return
            tgt = archive / p.name
            i = 1
            while tgt.exists():
                tgt = archive / f"{p.name}.{i}"
                i += 1
            shutil.move(str(p), str(tgt))
            moved.append(str(tgt))

        if key.startswith("sessions/"):
            sid = key.split("/", 1)[1]
            sf = pathlib.Path(_sw_cfg()["sessions_dir"]) / f"{sid}.json"
            try:
                sess = json.loads(sf.read_text())
            except Exception:
                sess = None
            if sess:
                for run in sess.get("runs") or []:
                    _move(pathlib.Path(b["folder"]) / run)
            _move(pathlib.Path(b["folder"]) / f"scan_session_{sid}.pdf")
            _move(sf)
        else:
            _move(pathlib.Path(b["folder"]))

        con.execute("UPDATE batches SET status = 'trashed' WHERE id = ?", (bid,))
        con.commit()
    finally:
        con.close()
    # Browser trash buttons post an EMPTY form body — request.form is then an
    # empty dict (falsy!). Branch on the content type instead: form posts (the
    # buttons) land back on the board; API/fetch clients still get JSON.
    if request.mimetype in ("application/x-www-form-urlencoded",
                            "multipart/form-data"):
        return redirect(url_for("board"))
    return jsonify(ok=True, moved=moved)


def main():
    db.init()
    app.run(host="0.0.0.0", port=9500, debug=False)


if __name__ == "__main__":
    main()
