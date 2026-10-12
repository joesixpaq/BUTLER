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
scans-root + scan-watch paths env-isolated so live dirs are never touched).
"""
import json
import os
import pathlib
import re
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

TMP = pathlib.Path(tempfile.mkdtemp(prefix="butler-batch-"))

# Isolate every path the web app could write to (trash archives, scan-watch
# state/sessions): the suite must never touch the live ~/scans or ~/lan-plan.
os.environ["BUTLER_SCANS_DIR"] = str(TMP / "scans")
os.environ["BUTLER_SW_SESSIONS"] = str(TMP / "scans" / "sessions")
os.environ["BUTLER_SW_STATE"] = str(TMP / "sw-state.json")
os.environ["BUTLER_SW_REQUEST"] = str(TMP / "sw-request.json")

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
    print()
    if FAIL:
        print(f"FAILURES: {FAIL}  (pass {PASS})")
        sys.exit(1)
    print(f"ALL PASS  ({PASS} checks)")
    sys.exit(0)


if __name__ == "__main__":
    main()
