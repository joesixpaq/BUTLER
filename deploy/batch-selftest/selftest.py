#!/usr/bin/env python3
"""Batch-view regression — letters as the unit of action (no hardware; temp DB).

Run from ~/BUTLER:   python3 deploy/batch-selftest/selftest.py
Exit 0 = ALL PASS.

Covers: one board card per letter (batch) · derived letter status
(inbox/analyzing/review/noise, user-move wins) · letter-wide status moves
(drag endpoint + DB sync) · per-scan removal (whole scan and single sides,
restore included) · exclusion respected by the analyzer (pending + sides)
· letter page contents (scans, removed list, payment, tasks, docs) · scan page
letter-status + removal banner · old per-sheet status route gone
· trash endpoint (form-post -> board redirect, API -> JSON, session archives;
scans-root + scan-watch paths env-isolated so live dirs are never touched)
· live board (auto-refresh hook, delegated interactions, reading-progress text)
· analysis gates (hold + schedule + analyze-now)
· intake race guard (settle window, open-session membership, duplicate self-heal)
· button-push intake (physical Scan button -> FTP PDF -> one letter: duplex
  pairing, settle window, idempotent, original kept)
· coverage hints (re-scan/duplicate detection via rotation-tolerant face
  hashes — shift and 180°-fed sheets still match; blank sides excluded;
  missing-page hints from page numbering, bilingual Seite/Page merged;
  archive fallback for trashed letters; chips on the board, section on the
  letter page; faces stay in sync with side removal).
"""
import json
import os
import pathlib
import re
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

TMP = pathlib.Path(tempfile.mkdtemp(prefix="butler-batch-"))

# Isolate every path the web app could write to (trash archives, scan-watch
# state/sessions): the suite must never touch the live ~/scans or ~/lan-plan.
os.environ["BUTLER_SCANS_DIR"] = str(TMP / "scans")
os.environ["BUTLER_SW_SESSIONS"] = str(TMP / "scans" / "sessions")
os.environ["BUTLER_SW_STATE"] = str(TMP / "sw-state.json")
os.environ["BUTLER_SW_REQUEST"] = str(TMP / "sw-request.json")
# analysis gates off by default in tests (gates have their own section)
os.environ["BUTLER_ANALYZE_HOLD"] = "0"
os.environ["BUTLER_ANALYZE_AT"] = ""

from butler import db  # noqa: E402

db.DB_PATH = TMP / "test.db"

from butler import analyze as A  # noqa: E402
from butler import tasks  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"PASS  {name}")
    else:
        FAIL += 1
        print(f"FAIL  {name}" + (f"  — {detail}" if detail else ""))


def eq(name, got, want):
    check(name, got == want, f"got={got!r} want={want!r}")


def col_block(html, st):
    m = re.search(r'<section class="lane" data-status="%s">(.*?)</section>' % st, html, re.S)
    return m.group(1) if m else ""


def make_run(folder, name):
    d = folder / "sheets"
    d.mkdir(parents=True, exist_ok=True)
    for f in name:
        (d / f).write_bytes(b"\xff\xd8fakejpg")


DOC = {"title": "Rechnung Test AG", "sender": "Test AG", "recipient": "D. M.",
       "language": "de", "category": "bill", "disposition": "action",
       "priority": "P1", "summary": "test invoice", "sides": ["front"],
       "content_notes": "", "facts": [], "needs_review": False, "review_reason": None}
NOISE_DOC = dict(DOC, title="Werbung XYZ", category="advert", disposition="noise",
                 priority="P3")
OFFICIAL_DOC = dict(DOC, title="Steueramt Schreiben", category="official",
                    disposition="waiting", priority="P2")


def seed():
    db.init()
    b1, b2, b3 = TMP / "b1", TMP / "b2", TMP / "b3"
    make_run(b1, ["sheet_01_front.jpg", "sheet_01_back.jpg",
                  "sheet_02_front.jpg", "sheet_02_back.jpg",
                  "sheet_03_front.jpg"])
    make_run(b2, ["sheet_01_front.jpg"])
    make_run(b3, ["sheet_01_front.jpg"])
    con = db.connect()
    con.execute("INSERT INTO batches(id,key,ts,source,folder,status,note,created_at) "
                "VALUES (1,'direct/pull_b1','20261012_100000','scanner',?,'registered','',0)",
                (str(b1),))
    con.execute("INSERT INTO batches(id,key,ts,source,folder,status,created_at) "
                "VALUES (2,'direct/pull_b2','20261012_110000','scanner',?,'registered',0)",
                (str(b2),))
    con.execute("INSERT INTO batches(id,key,ts,source,folder,status,created_at) "
                "VALUES (3,'direct/pull_b3','20261012_120000','scanner',?,'trashed',0)",
                (str(b3),))
    con.execute("INSERT INTO sheets(id,batch_id,sheet_no,front,back,status) VALUES "
                "(1,1,1,'sheets/sheet_01_front.jpg','sheets/sheet_01_back.jpg','review')")
    con.execute("INSERT INTO sheets(id,batch_id,sheet_no,front,back,status) VALUES "
                "(2,1,2,'sheets/sheet_02_front.jpg','sheets/sheet_02_back.jpg','inbox')")
    con.execute("INSERT INTO sheets(id,batch_id,sheet_no,front,back,status) VALUES "
                "(3,1,3,'sheets/sheet_03_front.jpg',NULL,'noise')")
    con.execute("INSERT INTO sheets(id,batch_id,sheet_no,front,back,status) VALUES "
                "(4,2,1,'sheets/sheet_01_front.jpg',NULL,'review')")
    con.execute("INSERT INTO sheets(id,batch_id,sheet_no,front,back,status) VALUES "
                "(5,3,1,'sheets/sheet_01_front.jpg',NULL,'review')")
    for sid, doc in ((1, DOC), (3, NOISE_DOC), (4, OFFICIAL_DOC), (5, DOC)):
        con.execute("INSERT INTO analyses(sheet_id,ts,model,status,json,raw,attempts) "
                    "VALUES (?,0,'test','ok',?,'',0)",
                    (sid, json.dumps({"documents": [doc], "sheet_notes": ""})))
    con.execute("INSERT INTO payments(sheet_id,ts,payload,data_json,check_json,status) "
                "VALUES (1,0,'SPC',?,?,'prepared')",
                (json.dumps({"amount": "52.00", "currency": "CHF",
                             "iban": "CH0830000001304878268",
                             "iban_fmt": "CH08 3000 0001 3048 7826 8",
                             "reference": "104010000069006810804912003",
                             "reference_fmt": "10 40100 00069 00681 08049 12003",
                             "ref_type": "QRR",
                             "creditor": {"name": "Post CH AG", "street": "W",
                                          "house": "4", "zip": "3030", "town": "Bern"},
                             "debtor": {"name": "E M"}}),
                 json.dumps({"amount": True, "iban": True, "reference": True, "notes": []})))
    con.commit()
    con.close()
    con = db.connect()
    con.execute("INSERT INTO tasks(sheet_id,title,kind,due,status,source,created_at) "
                "VALUES (1,'Zahlbar bis 04.11.2026','deadline','2026-11-04','open','analysis',0)")
    con.commit()
    con.close()


# --------------------------------------------------------------------------- #
def test_board(web):
    html = web.get("/").get_data(as_text=True)
    eq("A1 board 200", web.get("/").status_code, 200)
    eq("A2 one card per letter (2 letters, trashed hidden)",
       html.count('data-batch="'), 2)
    check("A3 trashed letter not on board", "pull_b3" not in html)
    check("A3b seven status lanes rendered", html.count('data-status="') == 7)
    check("A3c card opens in a new-tab hook", 'data-url="/batch/1"' in html
          and 'data-url="/batch/2"' in html)
    check("A4 B1 card in analyzing (inbox scan present)",
          'data-batch="1"' in col_block(html, "analyzing"))
    check("A5 B2 card in review", 'data-batch="2"' in col_block(html, "review"))
    check("A6 header counts", "2 letter(s) · 4 scan(s)" in html)
    check("A7 card shows scan count + due + pay chips",
          "3 scans" in col_block(html, "analyzing")
          and "due 04.11.2026" in html and "pay 52.00 CHF" in html)

    # all analyzed -> review; all noise -> noise; user move wins
    con = db.connect()
    con.execute("INSERT INTO analyses(sheet_id,ts,model,status,json,raw,attempts) "
                "VALUES (2,0,'test','ok',?,'',0)",
                (json.dumps({"documents": [DOC], "sheet_notes": ""}),))
    con.execute("UPDATE sheets SET status='review' WHERE id IN (1,2)")
    con.execute("UPDATE sheets SET status='review' WHERE id=3")
    con.commit()
    con.close()
    html = web.get("/").get_data(as_text=True)
    check("A8 all analyzed -> review", 'data-batch="1"' in col_block(html, "review"))

    con = db.connect()
    con.execute("UPDATE sheets SET status='noise' WHERE batch_id=1")
    con.commit()
    con.close()
    html = web.get("/").get_data(as_text=True)
    check("A9 all noise -> noise", 'data-batch="1"' in col_block(html, "noise"))

    con = db.connect()
    con.execute("UPDATE batches SET board_status='filed' WHERE id=1")
    con.commit()
    con.close()
    html = web.get("/").get_data(as_text=True)
    check("A10 user move wins over derivation", 'data-batch="1"' in col_block(html, "filed"))

    con = db.connect()
    con.execute("UPDATE batches SET board_status=NULL WHERE id=1")
    con.execute("UPDATE sheets SET status='review' WHERE id IN (1,2)")
    con.execute("UPDATE sheets SET status='noise' WHERE id=3")
    con.commit()
    con.close()


def test_status_endpoint(web):
    r = web.post("/api/batch/1/status", json={"status": "action"})
    eq("B1 batch status 200", r.status_code, 200)
    con = db.connect()
    bs = con.execute("SELECT board_status FROM batches WHERE id=1").fetchone()["board_status"]
    sts = {r["status"] for r in con.execute("SELECT status FROM sheets WHERE batch_id=1")}
    con.close()
    eq("B2 board_status stored", bs, "action")
    eq("B3 all scans synced", sts, {"action"})
    eq("B4 bad status rejected", web.post("/api/batch/1/status", json={"status": "nope"}).status_code, 400)
    eq("B5 unknown letter 404", web.post("/api/batch/999/status", json={"status": "filed"}).status_code, 404)
    r = web.post("/api/batch/1/status", data={"status": "filed"})
    eq("B6 form move redirects", r.status_code, 302)
    check("B7 redirect to letter page", "/batch/1" in r.headers.get("Location", ""))
    con = db.connect()
    con.execute("UPDATE batches SET board_status=NULL WHERE id=1")
    con.execute("UPDATE sheets SET status='review' WHERE id IN (1,2)")
    con.execute("UPDATE sheets SET status='noise' WHERE id=3")
    con.commit()
    con.close()


def test_exclude(web):
    # side removal
    r = web.post("/api/sheet/2/exclude", json={"scope": "back"})
    eq("C1 side exclude 200", r.status_code, 200)
    con = db.connect()
    flags = dict(con.execute("SELECT excluded, front_excluded, back_excluded FROM sheets WHERE id=2").fetchone())
    con.close()
    eq("C2 back flagged", flags, {"excluded": 0, "front_excluded": 0, "back_excluded": 1})
    html = web.get("/batch/1").get_data(as_text=True)
    check("C3 letter page shows removed back + restore", "back removed" in html and "show back" in html)
    web.post("/api/sheet/2/exclude", json={"scope": "back", "value": 0})
    html = web.get("/batch/1").get_data(as_text=True)
    check("C4 side restore hides it again", "back removed" not in html)
    eq("C5 bad scope rejected", web.post("/api/sheet/1/exclude", json={"scope": "middle"}).status_code, 400)
    eq("C6 unknown scan 404", web.post("/api/sheet/999/exclude", json={"scope": "scan"}).status_code, 404)

    # whole-scan removal
    web.post("/api/sheet/3/exclude", json={"scope": "scan"})
    html = web.get("/").get_data(as_text=True)
    check("C7 board scan count drops", "3 scan(s)" in html)
    check("C8 B1 card shows 2 scans", "2 scans" in col_block(html, "analyzing") or "2 scans" in html)
    html = web.get("/batch/1").get_data(as_text=True)
    check("C9 removed list + restore button", "Removed scans" in html and "restore scan" in html)
    web.post("/api/sheet/3/exclude", json={"scope": "scan", "value": 0})
    html = web.get("/batch/1").get_data(as_text=True)
    check("C10 restore brings the scan back", "Removed scans" not in html and "Werbung XYZ" in html)

    # all scans removed -> letter shows "no scans", status derives to noise
    for sid in (1, 2, 3):
        web.post(f"/api/sheet/{sid}/exclude", json={"scope": "scan"})
    html = web.get("/batch/1").get_data(as_text=True)
    check("C11 empty letter note", "no scans in this letter" in html)
    html = web.get("/").get_data(as_text=True)
    check("C12 empty letter under noise", 'data-batch="1"' in col_block(html, "noise"))
    for sid in (1, 2, 3):
        web.post(f"/api/sheet/{sid}/exclude", json={"scope": "scan", "value": 0})


def test_analyzer_paths(web):
    # reset scan 2 to a fresh pending state
    con = db.connect()
    con.execute("DELETE FROM analyses WHERE sheet_id=2")
    con.execute("UPDATE sheets SET status='inbox' WHERE id=2")
    con.commit()
    con.close()

    web.post("/api/sheet/2/exclude", json={"scope": "back"})
    from butler import analyze as AA
    check("D1 pending includes scan with front left", 2 in AA.pending_sheet_ids())

    captured = {}
    orig = AA.llm.analyze_images

    def fake(prompt, paths, system=None):
        captured["paths"] = [str(p) for p in paths]
        captured["prompt"] = prompt
        return {"text": json.dumps({"documents": [dict(DOC, title="Fake doc")],
                                    "sheet_notes": ""}),
                "model": "fake", "usage": {}, "provider": "fake"}

    AA.llm.analyze_images = fake
    try:
        r = AA.analyze_sheet(2, notify=False, verbose=False)
    finally:
        AA.llm.analyze_images = orig
    check("D2 analyze skips the removed back side",
          len(captured.get("paths", [])) == 1 and captured["paths"][0].endswith("sheet_02_front.jpg"))
    check("D3 prompt lists only the kept side",
          "Sides present: front (" in captured.get("prompt", ""))
    eq("D4 analyze succeeded", r.get("status"), "review")

    # both sides removed -> not pending, analyze is a clean no-op
    web.post("/api/sheet/2/exclude", json={"scope": "front"})
    check("D5 fully removed scan not pending", 2 not in AA.pending_sheet_ids())
    r = AA.analyze_sheet(2, notify=False, verbose=False)
    eq("D6 analyze on fully removed scan is skipped", r.get("status"), "skipped")
    web.post("/api/sheet/2/exclude", json={"scope": "front", "value": 0})
    web.post("/api/sheet/2/exclude", json={"scope": "back", "value": 0})


def test_letter_page(web):
    html = web.get("/batch/1").get_data(as_text=True)
    check("E1 letter page 200", web.get("/batch/1").status_code == 200)
    check("E2 scans section with thumbs", "Scans" in html and 'src="/img/1/front"' in html)
    check("E3 scan mail lines", "Rechnung Test AG" in html and "Werbung XYZ" in html)
    check("E4 status form", 'action="/api/batch/1/status"' in html)
    check("E5 trash form", "trash letter" in html)
    check("E6 payment block", "Payment prep" in html and "QR-IBAN" in html)
    check("E7 tasks aggregated", "Zahlbar bis 04.11.2026" in html and "<th>scan</th>" in html)
    check("E8 analysis section with scan refs", "open scan for the cited facts" in html)
    check("E9 scan links to scan page", "/sheet/1" in html)
    html3 = web.get("/batch/3").get_data(as_text=True)
    check("E10 trashed letter page banner", "was trashed" in html3)
    eq("E11 unknown letter 404", web.get("/batch/999").status_code, 404)

    html = web.get("/sheet/1").get_data(as_text=True)
    check("E12 scan page: letter status form", "letter status" in html
          and 'action="/api/batch/1/status"' in html)
    check("E13 scan page: back to letter", "← letter" in html)
    eq("E14 old per-sheet status route gone", web.post("/api/sheet/1/status", json={"status": "filed"}).status_code, 404)


def test_trash_endpoint(web):
    """Trash contract (form -> board, API -> JSON) + letter-page form returns."""
    # session-keyed letter: member runs + merged pdf + session json
    b4 = TMP / "b4"
    make_run(b4, ["sheet_01_front.jpg"])
    for run in ("pull_20261012_130001", "pull_20261012_130002"):
        (b4 / run).mkdir(parents=True, exist_ok=True)
    (b4 / "scan_session_20261012_130000.pdf").write_bytes(b"%PDF-fake")
    sess_dir = TMP / "scans" / "sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)
    (sess_dir / "20261012_130000.json").write_text(json.dumps(
        {"session": "20261012_130000",
         "runs": ["pull_20261012_130001", "pull_20261012_130002"]}))
    con = db.connect()
    con.execute("INSERT INTO batches(id,key,ts,source,folder,status,created_at) "
                "VALUES (4,'sessions/20261012_130000','20261012_130000','scanner',?,"
                "'registered',0)", (str(b4),))
    con.execute("INSERT INTO sheets(id,batch_id,sheet_no,front,back,status) "
                "VALUES (6,4,1,'sheets/sheet_01_front.jpg',NULL,'review')")
    con.commit()
    con.close()

    html = web.get("/").get_data(as_text=True)
    check("T1 active session letter on the board", 'data-batch="4"' in html)

    # the browser trash button: empty body + form content type -> back to the board
    r = web.post("/api/batch/4/trash", data="",
                 content_type="application/x-www-form-urlencoded")
    eq("T2 trash form post -> 302", r.status_code, 302)
    eq("T3 redirect target is the board", r.headers.get("Location"), "/")
    con = db.connect()
    st = con.execute("SELECT status FROM batches WHERE id=4").fetchone()["status"]
    con.close()
    eq("T4 letter marked trashed", st, "trashed")
    names = sorted(p.name for p in (TMP / "scans" / "archive").iterdir())
    eq("T5 runs + merged pdf + session json archived", names,
       ["20261012_130000.json", "pull_20261012_130001", "pull_20261012_130002",
        "scan_session_20261012_130000.pdf"])
    check("T6 run dirs + pdf gone from the session folder",
          not (b4 / "pull_20261012_130001").exists()
          and not (b4 / "pull_20261012_130002").exists()
          and not (b4 / "scan_session_20261012_130000.pdf").exists())
    check("T7 session json gone from the sessions dir",
          not (sess_dir / "20261012_130000.json").exists())
    html = web.get("/").get_data(as_text=True)
    check("T8 trashed letter drops off the board", 'data-batch="4"' not in html)

    # API clients (no form content type) still get JSON
    r = web.post("/api/batch/2/trash")
    eq("T9 API post -> 200", r.status_code, 200)
    j = r.get_json() or {}
    check("T10 JSON ok + moved paths",
          j.get("ok") is True and isinstance(j.get("moved"), list)
          and len(j["moved"]) >= 1 and all("archive" in m for m in j["moved"]))

    # mutation forms on the letter page come back to the letter
    r = web.post("/api/task/1/status", data={"status": "done"},
                 content_type="application/x-www-form-urlencoded")
    check("T11 task form -> letter page",
          "/batch/1" in r.headers.get("Location", ""))
    r = web.post("/api/payment/1/status", data={"status": "paid"},
                 content_type="application/x-www-form-urlencoded")
    check("T12 payment form -> letter page",
          "/batch/1" in r.headers.get("Location", ""))


def test_live_board(web):
    """Live board: fresh-feed cards show reading progress; refresh hooks shipped."""
    con = db.connect()
    con.execute("INSERT INTO batches(id,key,ts,source,folder,status,created_at) "
                "VALUES (5,'sessions/20261012_140000','20261012_140000','scanner',?,"
                "'registered',0)", (str(TMP / "b5"),))
    con.execute("INSERT INTO sheets(id,batch_id,sheet_no,front,back,status) VALUES "
                "(7,5,1,'sheets/s1_front.jpg','sheets/s1_back.jpg','analyzing')")
    con.execute("INSERT INTO sheets(id,batch_id,sheet_no,front,back,status) VALUES "
                "(8,5,2,'sheets/s2_front.jpg',NULL,'inbox')")
    con.commit()
    con.close()
    html = web.get("/").get_data(as_text=True)
    blk = col_block(html, "analyzing")
    check("L1 fresh-feed card in the analyzing lane", 'data-batch="5"' in blk)
    check("L2 card shows live reading progress", "reading scan 1 of 2…" in blk)
    js = web.get("/static/board.js").get_data(as_text=True)
    check("L3 board auto-refresh hook", "setInterval(refreshBoard" in js)
    check("L4 delegated card interactions", "closest('.card')" in js)


def test_analyze_gates(web):
    """Hold + schedule gates for analysis start, and the manual override."""
    import datetime as _dt
    import time as _time
    con = db.connect()
    con.execute("INSERT INTO batches(id,key,ts,source,folder,status,created_at,released) "
                "VALUES (6,'sessions/20261012_150000','20261012_150000','scanner',?,"
                "'registered',?,0)", (str(TMP / "b6"), _time.time()))
    con.execute("INSERT INTO sheets(id,batch_id,sheet_no,front,back,status) VALUES "
                "(9,6,1,'sheets/s3_front.jpg',NULL,'inbox')")
    con.commit()
    con.close()

    def set_gates(hold, at):
        os.environ["BUTLER_ANALYZE_HOLD"] = str(hold)
        os.environ["BUTLER_ANALYZE_AT"] = at

    def set_created(delta):
        con = db.connect()
        con.execute("UPDATE batches SET created_at=?, released=0 WHERE id=6",
                    (_time.time() + delta,))
        con.commit()
        con.close()

    try:
        set_gates(60, "")
        check("G1 hold gates a fresh letter", 9 not in A.pending_sheet_ids())
        set_gates(0, "")
        check("G2 hold 0 -> analyzable", 9 in A.pending_sheet_ids())

        ahead = (_dt.datetime.now() + _dt.timedelta(minutes=2)).strftime("%H:%M")
        past = (_dt.datetime.now() - _dt.timedelta(minutes=2)).strftime("%H:%M")
        set_created(0)
        set_gates(0, ahead)
        check("G3 schedule gates before the slot", 9 not in A.pending_sheet_ids())
        set_created(-3600)
        set_gates(0, past)
        check("G4 slot already passed -> analyzable", 9 in A.pending_sheet_ids())

        # board + letter page show the wait and offer the manual release
        set_created(0)
        set_gates(600, ahead)
        html = web.get("/").get_data(as_text=True)
        blk = col_block(html, "inbox")
        check("G5 card shows when analysis starts", 'data-batch="6"' in blk
              and "waiting for analysis — at " in blk)
        check("G6 inbox lane offers analyze-now", "analyze now" in blk)
        html = web.get("/batch/6").get_data(as_text=True)
        check("G7 letter page offers analyze-now", "analyze now" in html
              and "analysis starts at" in html)

        r = web.post("/api/analyze-now", data={"bid": "6"})
        eq("G8 form release -> 302 to letter", r.status_code, 302)
        check("G9 released despite gates", 9 in A.pending_sheet_ids())
        html = web.get("/batch/6").get_data(as_text=True)
        check("G10 countdown gone after release", "analysis starts at" not in html)
    finally:
        set_gates(0, "")


def test_intake_race(web):
    """Intake race guard: settle window + open-session membership + self-heal."""
    import butler.intake as ii
    root = TMP / "intake"
    direct = root / "scans" / "direct"
    sessions = root / "scans" / "sessions"
    direct.mkdir(parents=True)
    sessions.mkdir(parents=True)
    saved = (list(ii.SCAN_DIRS), ii.SESSIONS_DIR)
    ii.SCAN_DIRS = [direct, root / "scans" / "incoming"]
    ii.SESSIONS_DIR = sessions

    def mk_run(name, age=120):
        d = direct / name
        (d / "sheets").mkdir(parents=True, exist_ok=True)
        (d / "sheets" / "sheet_01_front.jpg").write_bytes(b"\xff\xd8\xff\xd9")
        mf = d / "manifest.json"
        mf.write_text(json.dumps({"scan_timestamp": name.split("_", 1)[1],
                                  "sheets": [{"index": 1,
                                              "front": "sheets/sheet_01_front.jpg",
                                              "back": ""}],
                                  "stream_end": "done"}))
        t = time.time() - age
        os.utime(mf, (t, t))
        return d

    def keys_of(new):
        return {k for k, _ in new}

    try:
        mk_run("pull_20261012_160001", age=0)
        new, _ = ii.ingest()
        check("N1 fresh run waits out the settle window",
              "direct/pull_20261012_160001" not in keys_of(new))
        mk_run("pull_20261012_160001", age=120)
        new, _ = ii.ingest()
        check("N2 settled run registers",
              "direct/pull_20261012_160001" in keys_of(new))

        mk_run("pull_20261012_160002", age=120)
        (sessions / "20261012_160000.json").write_text(json.dumps(
            {"session_id": "20261012_160000", "started": "2026-10-12T16:00:00",
             "started_ts": time.time() - 300, "closed": None, "source": "manual",
             "runs": ["pull_20261012_160002"], "sheets": 1}))
        new, _ = ii.ingest()
        check("N3 open session held + member run skipped",
              "sessions/20261012_160000" not in keys_of(new)
              and "direct/pull_20261012_160002" not in keys_of(new))

        # simulate the live race: standalone registers BEFORE the session file
        # lists the run, then the session registers late (2026-10-12 23:38)
        (sessions / "20261012_160000.json").unlink()
        new, _ = ii.ingest()
        check("N4 race sim: standalone registered first",
              "direct/pull_20261012_160002" in keys_of(new))
        (sessions / "20261012_160000.json").write_text(json.dumps(
            {"session_id": "20261012_160000", "started": "2026-10-12T16:00:00",
             "started_ts": time.time() - 300, "closed": "2026-10-12T16:01:00",
             "closed_reason": "user", "source": "manual",
             "runs": ["pull_20261012_160002"], "sheets": 1}))
        new, _ = ii.ingest()
        con = db.connect()
        st = con.execute("SELECT status,note FROM batches WHERE "
                         "key='direct/pull_20261012_160002'").fetchone()
        con.close()
        check("N5 session registers after close",
              "sessions/20261012_160000" in keys_of(new))
        check("N6 duplicate auto-retired, files intact",
              st["status"] == "trashed" and "duplicate" in (st["note"] or "")
              and (direct / "pull_20261012_160002" / "manifest.json").exists(),
              f"{st['status']} / {st['note']}")
    finally:
        ii.SCAN_DIRS, ii.SESSIONS_DIR = saved


def test_push_intake(web):
    """Physical-button pushes: incoming/*.pdf -> ONE letter batch."""
    import butler.intake as ii
    import butler.push as pp
    import butler.web as W
    import fitz

    root = TMP / "push"
    inc = root / "incoming"
    inc.mkdir(parents=True, exist_ok=True)
    sessions = root / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    saved = (pp.INCOMING, list(ii.SCAN_DIRS), ii.SESSIONS_DIR)
    pp.INCOMING = inc
    # keep ingest() away from the live scans dirs while testing the push path
    ii.SCAN_DIRS = [root / "direct", inc]
    ii.SESSIONS_DIR = sessions

    def mk_pdf(name, pages):
        d = fitz.open()
        for i in range(pages):
            pg = d.new_page(width=595, height=842)
            pg.insert_text((72, 100), f"Push test page {i + 1}")
        p = inc / name
        d.save(str(p))
        d.close()
        return p

    try:
        fresh = mk_pdf("Report_000001.pdf", 3)          # 3 pages -> 2 sheets
        new, _ = ii.ingest(notify=False)
        check("P1 fresh upload waits out the settle window",
              ("push/Report_000001", 2) not in new)
        t = time.time() - 60
        os.utime(fresh, (t, t))
        new, _ = ii.ingest(notify=False)
        check("P2 settled PDF -> one letter (3 pages -> 2 sheets)",
              ("push/Report_000001", 2) in new)

        folder = inc / "Report_000001"
        con = db.connect()
        b = con.execute("SELECT * FROM batches WHERE key='push/Report_000001'"
                        ).fetchone()
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM sheets WHERE batch_id=? ORDER BY sheet_no",
            (b["id"],))] if b else []
        con.close()
        check("P3 batch source/folder", b and (b["source"], b["folder"]) ==
              ("push", str(folder)), f"{b['source'] if b else None}")
        eq("P4 duplex front/back pairing",
           [(r["front"], r["back"]) for r in rows],
           [("sheets/sheet_01_front.jpg", "sheets/sheet_01_back.jpg"),
            ("sheets/sheet_02_front.jpg", "")])
        check("P5 rendered sides exist on disk",
              all((folder / r["front"]).exists() for r in rows)
              and (folder / "sheets/sheet_02_front.jpg").stat().st_size > 5000)
        check("P6 original PDF kept inside the folder + marker",
              (folder / "Report_000001.pdf").exists()
              and (folder / "push.json").exists())
        check("P7 sheets start in inbox (gates apply)",
              rows and all(r["status"] == "inbox" for r in rows))
        pdf_p = W._batch_pdf("push/Report_000001", str(folder))
        check("P8 letter PDF resolves for the push batch",
              pdf_p is not None and pdf_p.name == "Report_000001.pdf")

        new, _ = ii.ingest(notify=False)
        check("P9 idempotent — no duplicate on re-ingest", new == [])
        check("P10 push folder ignored by the generic run scanner",
              all(r.name != "Report_000001" for r in ii.find_runs([inc])))

        odd = mk_pdf("Estimate_000002.pdf", 5)          # odd tail page
        t = time.time() - 90
        os.utime(odd, (t, t))
        new, _ = ii.ingest(notify=False)
        con = db.connect()
        n = con.execute("SELECT COUNT(*) FROM sheets s JOIN batches b "
                        "ON b.id=s.batch_id WHERE b.key='push/Estimate_000002'"
                        ).fetchone()[0]
        con.close()
        check("P11 odd page count -> trailing front-only sheet",
              ("push/Estimate_000002", 3) in new and n == 3,
              f"new={new} n={n}")
    finally:
        pp.INCOMING, ii.SCAN_DIRS, ii.SESSIONS_DIR = saved


def test_coverage(web):
    """Coverage layer: rotation-tolerant re-scan detection + page hints."""
    from PIL import Image, ImageDraw
    import butler.coverage as cov

    base = TMP / "cov"
    root = base / "scans"
    cov.SCANS_ROOT = root          # archive fallback resolves under TMP

    def draw(kind, path, rot=0, shift=(0, 0)):
        im = Image.new("L", (820, 1160), 255)
        d = ImageDraw.Draw(im)
        if kind == "a":                      # header + dense text body
            d.rectangle([70, 90, 750, 150], fill=0)
            for i in range(12):
                d.rectangle([70, 240 + 60 * i, 640 - 8 * i, 252 + 60 * i], fill=0)
        elif kind == "b":                    # left column + footer band
            d.rectangle([70, 980, 750, 1030], fill=0)
            for i in range(7):
                d.rectangle([70, 160 + 95 * i, 170 + 45 * i, 190 + 95 * i], fill=0)
        elif kind == "c":                    # one big dark block (different mail)
            d.rectangle([90, 100, 730, 420], fill=0)
            d.rectangle([90, 470, 430, 520], fill=0)
        elif kind == "speck":                # near-white with a small light mark
            d.rectangle([120, 980, 150, 1010], fill=120)
        # "blank": stays pure white
        if shift != (0, 0):
            canvas = Image.new("L", im.size, 255)
            canvas.paste(im, shift)
            im = canvas
        if rot:
            im = im.rotate(rot)
        im.convert("RGB").save(str(path), quality=88)

    for tag in ("x", "x2", "y"):
        (base / tag / "sheets").mkdir(parents=True, exist_ok=True)
    fx, fx2, fy = base / "x", base / "x2", base / "y"
    draw("a", fx / "sheets/s1f.jpg")
    draw("blank", fx / "sheets/s1b.jpg")
    draw("b", fx / "sheets/s2f.jpg")
    draw("blank", fx / "sheets/s2b.jpg")
    draw("a", fx2 / "sheets/s1f.jpg", shift=(4, -3))     # re-scan: shifted
    draw("speck", fx2 / "sheets/s1b.jpg")                # different near-blank back
    draw("b", fx2 / "sheets/s2f.jpg", rot=180)           # re-scan: upside-down sheet
    draw("blank", fx2 / "sheets/s2b.jpg")
    draw("c", fy / "sheets/s1f.jpg")                     # different mail
    draw("blank", fy / "sheets/s1b.jpg")

    con = db.connect()

    def mk_batch(key, folder, sides, status="registered"):
        cur = con.execute(
            "INSERT INTO batches(key,ts,source,folder,status,note,created_at) "
            "VALUES (?,'20261013_090000','scanner',?,?,'',0)", (key, str(folder), status))
        bid = cur.lastrowid
        sids = []
        for i, (f, b) in enumerate(sides, 1):
            cur = con.execute(
                "INSERT INTO sheets(batch_id,sheet_no,front,back,status) "
                "VALUES (?,?,?,?,'review')", (bid, i, f, b))
            sids.append(cur.lastrowid)
        return bid, sids

    PAIR = [("sheets/s1f.jpg", "sheets/s1b.jpg"), ("sheets/s2f.jpg", "sheets/s2b.jpg")]
    bx, sx = mk_batch("cov/x", fx, PAIR)
    bx2, sx2 = mk_batch("cov/x2", fx2, PAIR)
    by, sy = mk_batch("cov/y", fy, [("sheets/s1f.jpg", "sheets/s1b.jpg")])
    # archived letter: live folder is gone, files live in <scans>/archive/<name>/
    bz, sz = mk_batch("cov/arch", base / "gone", [("sheets/s1f.jpg", None)])
    con.commit()
    con.close()
    (root / "archive" / "gone" / "sheets").mkdir(parents=True, exist_ok=True)
    draw("a", root / "archive" / "gone" / "sheets" / "s1f.jpg")

    eq("CV0 non-image files are skipped silently", cov.index_batch(1), 0)
    eq("CV1 index writes one face per kept side", cov.index_batch(bx), 4)
    eq("CV2 X2 indexed", cov.index_batch(bx2), 4)
    eq("CV3 Y indexed", cov.index_batch(by), 2)
    con = db.connect()
    eq("CV4 blank backs flagged not-matchable",
       con.execute("SELECT COUNT(*) FROM faces WHERE batch_id=? AND blank=1",
                   (bx,)).fetchone()[0], 2)
    con.close()

    d = cov.hints_for(bx2)["dups"]
    check("CV5 re-scan detected (shift + 180° sheet still matches)",
          len(d) == 1 and d[0]["other"] == bx and d[0]["matched"] == 2
          and d[0]["mine"] == 2 and not d[0]["trashed"], str(d))
    check("CV6 different mail -> no dup hint", cov.hints_for(by)["dups"] == [],
          str(cov.hints_for(by)["dups"]))
    check("CV7 symmetric: X flags X2",
          any(x["other"] == bx2 for x in cov.hints_for(bx)["dups"]))
    eq("CV8 archived letter still indexable", cov.index_batch(bz), 1)

    # page hints: complete numbering -> silent; gap -> reported, bilingual merged
    con = db.connect()
    for sid, val in ((sx2[0], "Seite 2/4"), (sx2[1], "Seite 3/4"), (sx2[1], "Seite 4/4")):
        con.execute("INSERT INTO facts(sheet_id,doc_idx,kind,value,citation,confidence) "
                    "VALUES (?,0,'other',?,'test',1.0)", (sid, val))
    con.execute("INSERT INTO analyses(sheet_id,ts,model,status,json,raw,attempts) "
                "VALUES (?,0,'test','ok',?,?,0)",
                (sx2[0], json.dumps({"documents": [DOC], "sheet_notes": ""}),
                 "... Page 2/4 ... and Seite 2/4 ..."))
    for sid, val in zip(sx, ("Seite 1/2", "Seite 2/2")):
        con.execute("INSERT INTO facts(sheet_id,doc_idx,kind,value,citation,confidence) "
                    "VALUES (?,0,'other',?,'test',1.0)", (sid, val))
    con.commit()
    con.close()
    p = cov.hints_for(bx2)["pages"]
    check("CV9 page gap reported, Seite/Page merged",
          len(p) == 1 and p[0]["label"] == "Seite" and p[0]["seen"] == [2, 3, 4]
          and p[0]["missing"] == [1] and p[0]["missing_txt"] == "1/4", str(p))
    eq("CV10 complete numbering -> no page hint", cov.hints_for(bx)["pages"], [])

    html = web.get("/").get_data(as_text=True)
    check("CV11 board card shows dup chip",
          f"dup of #{bx}?" in html and "missing 1/4?" in html)
    con = db.connect()
    con.execute("UPDATE batches SET status='trashed' WHERE id=?", (bx,))
    con.commit()
    con.close()
    html = web.get("/").get_data(as_text=True)
    check("CV12 trashed duplicate target marked on the chip",
          f"dup of #{bx} (trashed)?" in html)
    page = web.get(f"/batch/{bx2}").get_data(as_text=True)
    check("CV13 letter page coverage section",
          "Coverage checks" in page
          and f'sides also captured in letter <a href="/batch/{bx}">#{bx}</a> (trashed)'
          in page
          and "<b>1/4</b> not found in this letter" in page)

    r = web.post(f"/api/sheet/{sx2[0]}/exclude", json={"scope": "front"})
    con = db.connect()
    gone = con.execute("SELECT COUNT(*) FROM faces WHERE sheet_id=? AND side='front'",
                       (sx2[0],)).fetchone()[0]
    con.close()
    eq("CV14 removed side drops its face row", (r.status_code, gone), (200, 0))
    web.post(f"/api/sheet/{sx2[0]}/exclude", json={"scope": "front", "value": 0})
    con = db.connect()
    back = con.execute("SELECT COUNT(*) FROM faces WHERE sheet_id=? AND side='front'",
                       (sx2[0],)).fetchone()[0]
    con.close()
    eq("CV15 restored side re-indexed", back, 1)


def main():
    seed()
    from butler.web import app
    web = app.test_client()
    test_board(web)
    test_status_endpoint(web)
    test_exclude(web)
    test_analyzer_paths(web)
    test_letter_page(web)
    test_trash_endpoint(web)
    test_live_board(web)
    test_analyze_gates(web)
    test_intake_race(web)
    test_push_intake(web)
    test_coverage(web)
    print()
    if FAIL:
        print(f"FAILURES: {FAIL}  (pass {PASS})")
        sys.exit(1)
    print(f"ALL PASS  ({PASS} checks)")
    sys.exit(0)


if __name__ == "__main__":
    main()
