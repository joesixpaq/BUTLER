"""BUTLER board — Kanban browser for scanned mail (v1, P2: analysis-aware).

Run:  python3 -m butler.web      (systemd: butler-board.service, port 9500)

Columns = sheet statuses. Cards show the analyzed mail (title, chips, key
facts); detail view = front/back images + full analysis (cited facts).
"""
import json
import pathlib

from flask import (Flask, abort, jsonify, redirect, render_template,
                   request, send_file, url_for)

from . import ROOT, db

app = Flask(__name__, template_folder=str(ROOT / "templates"),
            static_folder=str(ROOT / "static"))


def _sheet_row(sid):
    con = db.connect()
    try:
        return con.execute(
            "SELECT s.*, b.folder, b.key AS batch_key, b.ts AS batch_ts, b.note AS batch_note, "
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


def _batch_pdf(folder):
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
            "LEFT JOIN analyses a ON a.sheet_id = s.id ORDER BY s.id")]
        n_batches = con.execute("SELECT COUNT(*) FROM batches").fetchone()[0]
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
    pdf = _batch_pdf(s["folder"])
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
    p = _batch_pdf(b["folder"])
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


def main():
    db.init()
    app.run(host="0.0.0.0", port=9500, debug=False)


if __name__ == "__main__":
    main()
