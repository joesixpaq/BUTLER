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

from flask import (Flask, abort, jsonify, redirect, render_template,
                   request, send_file, url_for)

from . import ROOT, db

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


@app.get("/")
def board():
    con = db.connect()
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT s.*, b.key AS batch_key, b.ts AS batch_ts, "
            "a.status AS astatus, a.error AS aerror, a.json AS ajson "
            "FROM sheets s JOIN batches b ON b.id = s.batch_id "
            "LEFT JOIN analyses a ON a.sheet_id = s.id "
            "WHERE COALESCE(b.status, '') != 'trashed' "
            "ORDER BY s.id")]
        n_batches = con.execute(
            "SELECT COUNT(*) FROM batches WHERE COALESCE(status, '') != 'trashed'"
        ).fetchone()[0]
    finally:
        con.close()
    for r in rows:
        r["analysis"] = None
        r["docs"] = []
        if r.get("ajson"):
            try:
                r["analysis"] = json.loads(r["ajson"])
                r["docs"] = r["analysis"].get("documents") or []
            except (TypeError, json.JSONDecodeError):
                pass
    cols = {st: [] for st in db.SHEET_STATUSES}
    for r in rows:
        cols.setdefault(r["status"], []).append(r)
    return render_template("board.html", cols=cols, statuses=db.SHEET_STATUSES,
                           total=len(rows), batches=n_batches)


@app.get("/sheet/<int:sid>")
def sheet_view(sid):
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
    finally:
        con.close()
    return render_template("sheet.html", s=s, statuses=db.SHEET_STATUSES, pdf=pdf,
                           analysis=analysis, facts=facts)


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


@app.post("/api/sheet/<int:sid>/status")
def set_status(sid):
    st = (request.get_json(silent=True) or {}).get("status") or request.form.get("status")
    if st not in db.SHEET_STATUSES:
        abort(400, description=f"status must be one of {db.SHEET_STATUSES}")
    con = db.connect()
    try:
        con.execute("UPDATE sheets SET status = ? WHERE id = ?", (st, sid))
        con.commit()
    finally:
        con.close()
    if request.form:
        return redirect(url_for("sheet_view", sid=sid))
    return jsonify(ok=True, status=st)


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
    if request.form:
        return redirect(url_for("board"))
    return jsonify(ok=True, moved=moved)


def main():
    db.init()
    app.run(host="0.0.0.0", port=9500, debug=False)


if __name__ == "__main__":
    main()
