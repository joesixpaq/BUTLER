#!/usr/bin/env python3
"""P3 regression — deadlines/reminders/payments (no hardware; temp DB + dirs).

Run from ~/BUTLER:   python3 deploy/p3-selftest/selftest.py
Exit 0 = ALL PASS.

Covers: German/ISO date parsing · Swiss QR (SPC) parsing + formatting ·
cross-check vs analysis facts · REAL decode of the live Post CH payment slip
(if present) · deadline extraction (idempotent, trashed-batch guard, late
backfill silence) · reminder engine (offsets 14/7/3/1, catch-up, overdue once,
retry on failed send, dry mode, done/cancelled/trashed ignored) · .ics export +
route · board API (task add/status, payment status, ics download) + rendered
chips/blocks.
"""
import json
import pathlib
import re
import sys
import tempfile
from datetime import date, timedelta

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

TMP = pathlib.Path(tempfile.mkdtemp(prefix="butler-p3-"))
TODAY = date.today()

from butler import db  # noqa: E402

db.DB_PATH = TMP / "test.db"

from butler import tasks, qrpay  # noqa: E402

# live artifacts (optional — the real Post CH invoice re-scan of 2026-10-11;
# if the run has since been trashed, use its archived copy — same fixture)
LIVE_RUN = pathlib.Path.home() / "scans/direct/pull_20261011_235058"
LIVE_IMG = LIVE_RUN / "sheets/sheet_01_front.jpg"
if not LIVE_IMG.exists():
    _arch = pathlib.Path.home() / "scans/archive/pull_20261011_235058"
    if (_arch / "sheets/sheet_01_front.jpg").exists():
        LIVE_RUN, LIVE_IMG = _arch, _arch / "sheets/sheet_01_front.jpg"

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


# --------------------------------------------------------------------------- #
# seed: temp registry (sheet 1 -> live folder; sheet 2 -> nothing; sheet 3 -> trashed batch)
# --------------------------------------------------------------------------- #
def seed():
    db.init()
    con = db.connect()
    con.execute("INSERT INTO batches(id,key,ts,source,folder,status,note,created_at) "
                "VALUES (1,'direct/pull_test','20261011_235058','scanner',?,'registered','test',0)",
                (str(LIVE_RUN),))
    con.execute("INSERT INTO sheets(id,batch_id,sheet_no,front,back,status,meta_json) "
                "VALUES (1,1,1,'sheets/sheet_01_front.jpg','sheets/sheet_01_back.jpg','review','{}')")
    con.execute("INSERT INTO sheets(id,batch_id,sheet_no,front,back,status,meta_json) "
                "VALUES (2,1,2,NULL,NULL,'review','{}')")
    con.execute("INSERT INTO batches(id,key,ts,source,folder,status,created_at) "
                "VALUES (2,'direct/pull_trash','20261010_000000','scanner',?,'trashed',0)",
                (str(TMP / 'trashrun'),))
    con.execute("INSERT INTO sheets(id,batch_id,sheet_no,front,back,status,meta_json) "
                "VALUES (3,2,1,NULL,NULL,'review','{}')")
    # analysis + facts mirroring the real Post CH invoice (for cross-check/ics titles)
    analysis = {"documents": [{"title": "Rechnung Post CH AG 2309642487 (Post umleiten)"}],
                "sheet_notes": ""}
    con.execute("INSERT INTO analyses(sheet_id,ts,model,status,json,raw,attempts) "
                "VALUES (1,0,'test','ok',?,'',0)", (json.dumps(analysis),))
    facts = [
        (1, 0, "amount_due", "CHF 52.00"),
        (1, 0, "account", "CH08 3000 0001 3048 7826 8"),
        (1, 0, "account", "IBAN CH57 0900 0000 3048 7826 8"),
        (1, 0, "reference", "Zahlreferenz 1080491200"),
        (1, 0, "deadline", "Zahlbar bis 04.11.2026"),
    ]
    for sid, di, kind, val in facts:
        con.execute("INSERT INTO facts(sheet_id,doc_idx,kind,value,citation,confidence) "
                    "VALUES (?,?,?,?,'test',0.9)", (sid, di, kind, val))
    con.execute("INSERT INTO facts(sheet_id,doc_idx,kind,value,citation,confidence) "
                "VALUES (3,0,'deadline','31. Juli 2026','test',0.9)")
    # payment row in the exact shape qrpay.decode_for_sheet stores (status
    # 'prepared'), so the UI checks (H4/H6/H7 + H15/H16) never depend on the
    # optional live fixture; D* re-decodes and upserts on top when it exists.
    pdata = qrpay.parse_spc(SPC_REAL)
    pdata["iban_fmt"] = qrpay.fmt_iban(pdata["iban"])
    pdata["reference_fmt"] = qrpay.fmt_ref(pdata["ref_type"], pdata["reference"])
    pcheck = qrpay.crosscheck(pdata, [
        {"kind": "amount_due", "value": "CHF 52.00"},
        {"kind": "account", "value": "IBAN CH57 0900 0000 3048 7826 8"},
        {"kind": "reference", "value": "Zahlreferenz 1080491200"},
    ])
    con.execute("INSERT INTO payments(sheet_id, ts, payload, data_json, check_json, "
                "status) VALUES (1, 0, ?, ?, ?, 'prepared')",
                ("SPC", json.dumps(pdata, ensure_ascii=False),
                 json.dumps(pcheck, ensure_ascii=False)))
    con.commit()
    con.close()


# --------------------------------------------------------------------------- #
# A. date parsing
# --------------------------------------------------------------------------- #
def test_dates():
    eq("A1 dd.mm.yyyy embedded", tasks.parse_date("Zahlbar bis 04.11.2026"), date(2026, 11, 4))
    eq("A2 German month name", tasks.parse_date("31. Juli 2026"), date(2026, 7, 31))
    eq("A3 ISO", tasks.parse_date("2026-11-04"), date(2026, 11, 4))
    eq("A4 1-digit day + Monat", tasks.parse_date("1. Januar 2027"), date(2027, 1, 1))
    eq("A5 first date of a range", tasks.parse_date("01.09.2026 - 30.09.2026"), date(2026, 9, 1))
    eq("A6 no date", tasks.parse_date("Betreff ohne Datum"), None)
    eq("A7 invalid date", tasks.parse_date("32.13.2026"), None)
    eq("A8 fmt_date", tasks.fmt_date(date(2026, 11, 4)), "04.11.2026")


# --------------------------------------------------------------------------- #
# B. SPC parse + formatting
# --------------------------------------------------------------------------- #
SPC_REAL = ("SPC\r\n0200\r\n1\r\nCH0830000001304878268\r\nS\r\nPost CH AG, Log.-Services\r\n"
            "Wankdorfallee\r\n4\r\n3030\r\nBern\r\nCH\r\n\r\n\r\n\r\n\r\n\r\n\r\n\r\n"
            "52.00\r\nCHF\r\nS\r\nEkaterina Makhrova\r\nEigenstrasse\r\n7\r\n8700\r\n"
            "Küsnacht ZH\r\nCH\r\nQRR\r\n104010000069006810804912003\r\n\r\nEPD\r\n\r\n")


def test_spc():
    d = qrpay.parse_spc(SPC_REAL)
    eq("B1 amount", d["amount"], "52.00")
    eq("B2 currency", d["currency"], "CHF")
    eq("B3 iban", d["iban"], "CH0830000001304878268")
    eq("B4 creditor name", d["creditor"]["name"], "Post CH AG, Log.-Services")
    eq("B5 debtor name", d["debtor"]["name"], "Ekaterina Makhrova")
    eq("B6 ref type/ref", (d["ref_type"], d["reference"]),
       ("QRR", "104010000069006810804912003"))

    fields = (["SPC", "0200", "1", "CH5800791123000889012", "S", "Creditor AG",
               "Street", "1", "8000", "Zürich", "CH"] + [""] * 7
              + ["123.45", "CHF"] + [""] * 7
              + ["SCOR", "RF18539007547034", "", "EPD"])
    d2 = qrpay.parse_spc("\n".join(fields))  # LF endings + trailing fields omitted
    eq("B7 short/low-field payload amount", (d2["amount"], d2["ref_type"]),
       ("123.45", "SCOR"))

    eq("B8 fmt_iban", qrpay.fmt_iban("CH0830000001304878268"), "CH08 3000 0001 3048 7826 8")
    eq("B9 fmt_ref QRR", qrpay.fmt_ref("QRR", "104010000069006810804912003"),
       "10 40100 00069 00681 08049 12003")
    eq("B10 fmt_ref SCOR", qrpay.fmt_ref("SCOR", "RF18539007547034"), "RF18 5390 0754 7034")


# --------------------------------------------------------------------------- #
# C. cross-check vs facts
# --------------------------------------------------------------------------- #
def test_crosscheck():
    data = qrpay.parse_spc(SPC_REAL)
    good = [
        {"kind": "amount_due", "value": "CHF 52.00"},
        {"kind": "account", "value": "IBAN CH57 0900 0000 3048 7826 8"},   # different IBAN prefix, same tail
        {"kind": "reference", "value": "Zahlreferenz 1080491200"},
    ]
    x = qrpay.crosscheck(data, good)
    eq("C1 amount ok", x["amount"], True)
    eq("C2 iban tail ok (QR-IBAN vs IBAN)", x["iban"], True)
    eq("C3 reference ok", x["reference"], True)
    check("C4 QR-IBAN note present", any("QR-IBAN" in n for n in x["notes"]), str(x["notes"]))
    x2 = qrpay.crosscheck(data, [{"kind": "amount_due", "value": "CHF 99.00"},
                                 {"kind": "account", "value": "CH57 0900 0000 1111 2222 3"}])
    eq("C5 amount mismatch", x2["amount"], False)
    eq("C6 iban mismatch", x2["iban"], False)
    x3 = qrpay.crosscheck(data, [])
    eq("C7 no facts -> None checks", (x3["amount"], x3["iban"], x3["reference"]),
       (None, None, None))


# --------------------------------------------------------------------------- #
# D. real image decode (live Post CH slip)
# --------------------------------------------------------------------------- #
def test_real_decode():
    if not LIVE_IMG.exists():
        print("SKIP  D* live image not present — real-decode checks skipped")
        return None
    r = qrpay.decode_for_sheet(1)
    check("D1 payment row created", r is not None)
    if not r:
        return None
    eq("D2 real amount", r["data"]["amount"], "52.00")
    eq("D3 real iban", r["data"]["iban"], "CH0830000001304878268")
    eq("D4 real ref formatted", r["data"]["reference_fmt"], "10 40100 00069 00681 08049 12003")
    eq("D5 checks all ok", (r["check"]["amount"], r["check"]["iban"], r["check"]["reference"]),
       (True, True, True))
    eq("D6 status prepared", r["status"], "prepared")
    p = qrpay.payment_for_sheet(1)
    check("D7 display dict + copy text", p and "QR-IBAN: CH08 3000 0001 3048 7826 8" in p["copy_text"])
    # re-decode keeps status (idempotent upsert)
    qrpay.set_payment_status(r["id"], "paid")
    qrpay.decode_for_sheet(1)
    eq("D8 re-decode keeps paid", qrpay.payment_for_sheet(1)["status"], "paid")
    qrpay.set_payment_status(r["id"], "prepared")
    return r


# --------------------------------------------------------------------------- #
# E. extraction
# --------------------------------------------------------------------------- #
def test_extract():
    new = tasks.extract_for_sheet(1)
    eq("E1 deadline -> task", len(new), 1)
    con = db.connect()
    t = con.execute("SELECT * FROM tasks WHERE id=?", (new[0],)).fetchone()
    con.close()
    eq("E2 task due", t["due"], "2026-11-04")
    eq("E3 task kind/source", (t["kind"], t["source"]), ("deadline", "analysis"))
    eq("E4 idempotent re-extract", tasks.extract_for_sheet(1), [])
    eq("E5 trashed batch ignored", tasks.extract_for_sheet(3), [])
    # late backfill silence: a long-past deadline is created but pre-marked
    con = db.connect()
    con.execute("INSERT INTO facts(sheet_id,doc_idx,kind,value,citation,confidence) "
                "VALUES (1,0,'deadline','01.02.2020','test',0.9)")
    con.commit()
    con.close()
    late = tasks.extract_for_sheet(1)
    eq("E6 late task created", len(late), 1)
    con = db.connect()
    sent = {r["offset_days"] for r in con.execute(
        "SELECT offset_days FROM reminders WHERE task_id=?", (late[0],))}
    con.close()
    eq("E7 late task pre-silenced", sent, {14, 7, 3, 1, -1})
    tasks.set_task_status(late[0], "cancelled")  # keep the board fixture minimal


# --------------------------------------------------------------------------- #
# F. reminder engine
# --------------------------------------------------------------------------- #
def test_reminders():
    sent_msgs = []
    fail = {"on": False}

    def fake(t):
        if fail["on"]:
            return False
        sent_msgs.append(t)
        return True

    tasks.send_telegram = fake

    def add(title, due, sid=1):
        return tasks.add_task(sid, title, due=due)

    # F1: 14-day fire + dedupe
    t1 = add("T1 fourteen", TODAY + timedelta(days=14))
    out = tasks.run_reminders(asof=TODAY)
    eq("F1 T-14 fires once", len(out), 1)
    check("F2 message says in 14 days", "in 14 days" in out[0])
    eq("F3 second pass silent", tasks.run_reminders(asof=TODAY), [])

    # F4..: walk T2 down the offsets
    t2 = add("T2 catchup", TODAY + timedelta(days=6))
    out = tasks.run_reminders(asof=TODAY)
    eq("F4 late create fires at 6 days", len(out), 1)
    check("F5 says in 6 days", "in 6 days" in out[0])
    eq("F6 no double at d=5", tasks.run_reminders(asof=TODAY + timedelta(days=1)), [])
    out = tasks.run_reminders(asof=TODAY + timedelta(days=3))
    check("F7 fires at d=3", len(out) == 1 and "in 3 days" in out[0])
    eq("F8 silent at d=2", tasks.run_reminders(asof=TODAY + timedelta(days=4)), [])
    out = tasks.run_reminders(asof=TODAY + timedelta(days=5))
    check("F9 tomorrow at d=1", len(out) == 1 and "tomorrow" in out[0])
    eq("F10 silent at d=0", tasks.run_reminders(asof=TODAY + timedelta(days=6)), [])
    out = tasks.run_reminders(asof=TODAY + timedelta(days=7))
    check("F11 overdue once", any("PASSED" in m and "1 day overdue" in m for m in out))
    check("F12 overdue not repeated",
          not any("PASSED" in m for m in tasks.run_reminders(asof=TODAY + timedelta(days=8))))

    # F13: failed send is retried, not marked
    t3 = add("T3 retry", TODAY + timedelta(days=7))
    fail["on"] = True
    eq("F13 failed send -> not counted", tasks.run_reminders(asof=TODAY), [])
    fail["on"] = False
    out = tasks.run_reminders(asof=TODAY)
    eq("F14 retried next pass", len(out), 1)

    # F15: dry mode never marks
    t4 = add("T4 dry", TODAY + timedelta(days=14))
    eq("F15 dry returns message", len(tasks.run_reminders(asof=TODAY, dry=True)), 1)
    eq("F16 dry marks nothing", len(tasks.run_reminders(asof=TODAY, dry=True)), 1)

    # F17: done / cancelled / trashed are ignored
    t5 = add("T5 done", TODAY + timedelta(days=3))
    tasks.set_task_status(t5, "done")
    t6 = add("T6 cancelled", TODAY + timedelta(days=3))
    tasks.set_task_status(t6, "cancelled")
    t7 = add("T7 trashed", TODAY + timedelta(days=3), sid=3)
    check("F17 closed/trashed silent",
          all("T5" not in m and "T6" not in m and "T7" not in m
              for m in tasks.run_reminders(asof=TODAY)))

    # F18: payment line appears for a prepared payment on the same sheet
    t8 = None
    if qrpay.payment_for_sheet(1):
        t8 = add("T8 pay", TODAY + timedelta(days=1))
        out = tasks.run_reminders(asof=TODAY)
        check("F18 payment line in reminder",
              any("payment prepared: 52.00 CHF" in m for m in out))
        # F19: deadline task on a SIBLING sheet of the same batch finds the
        # payment part (invoice sheet 5 + payment slip sheet 7 pattern)
        t9 = tasks.add_task(2, "T9 sibling", due=TODAY + timedelta(days=1))
        out = tasks.run_reminders(asof=TODAY)
        check("F19 batch-sibling payment line",
              any("payment prepared: 52.00 CHF" in m and "(sheet 1)" in m for m in out))
        tasks.set_task_status(t9, "cancelled")

    # keep the board fixture minimal: close everything except the E1 deadline
    for tid in (t1, t2, t3, t4, t5, t6, t8):
        if tid:
            tasks.set_task_status(tid, "done")


# --------------------------------------------------------------------------- #
# G. .ics
# --------------------------------------------------------------------------- #
def test_ics():
    con = db.connect()
    t = con.execute("SELECT * FROM tasks WHERE due='2026-11-04' AND kind='deadline'").fetchone()
    con.close()
    ics = tasks.make_ics(t, mail_title="Rechnung Post CH AG")
    check("G1 VCALENDAR wrapper", ics.startswith("BEGIN:VCALENDAR") and "END:VCALENDAR" in ics)
    check("G2 CRLF endings", "\r\n" in ics and not re.search(r"[^\r]\n", ics))
    check("G3 all-day DTSTART", "DTSTART;VALUE=DATE:20261104\r\n" in ics)
    check("G4 due +1 as DTEND", "DTEND;VALUE=DATE:20261105\r\n" in ics)
    check("G5 UID", f"UID:butler-task-{t['id']}@" in ics)
    check("G6 summary + alarm", "SUMMARY:Deadline: Zahlbar bis 04.11.2026" in ics
          and "TRIGGER:-P1D" in ics)
    return t


# --------------------------------------------------------------------------- #
# H. board (Flask test client)
# --------------------------------------------------------------------------- #
def test_board(ics_task):
    from butler.web import app
    c = app.test_client()

    r = c.get("/")
    eq("H1 board 200", r.status_code, 200)
    html = r.get_data(as_text=True)
    check("H2 card due chip", "due 04.11.2026" in html)
    check("H3 header task counter", "open task(s)" in html)
    check("H4 pay chip on board", "pay 52.00 CHF" in html)

    r = c.get("/sheet/1")
    eq("H5 sheet 200", r.status_code, 200)
    html = r.get_data(as_text=True)
    check("H6 payment block", "Payment prep" in html and "QR-IBAN" in html)
    check("H7 copy text present", "QR-IBAN: CH08 3000 0001 3048 7826 8" in html)
    check("H8 tasks block", "Tasks &amp; reminders" in html and "Zahlbar bis 04.11.2026" in html)
    check("H9 ics link", f"/ics/{ics_task['id']}" in html)

    r = c.post("/api/sheet/1/task", json={"title": "manual task", "due": "2026-12-01"})
    eq("H10 add task json 200", r.status_code, 200)
    tid = r.get_json()["id"]
    r = c.post("/api/sheet/1/task", json={"title": "bad", "due": "garbage"})
    eq("H11 bad due rejected", r.status_code, 400)
    r = c.post(f"/api/task/{tid}/status", json={"status": "done"})
    eq("H12 task done 200", r.status_code, 200)
    con = db.connect()
    st = con.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()["status"]
    con.close()
    eq("H13 task status stored", st, "done")
    r = c.post(f"/api/task/{tid}/status", json={"status": "nope"})
    eq("H14 bad task status rejected", r.status_code, 400)

    p = qrpay.payment_for_sheet(1)
    if p:
        r = c.post(f"/api/payment/{p['id']}/status", json={"status": "paid"})
        eq("H15 payment paid 200", r.status_code, 200)
        eq("H16 payment status stored", qrpay.payment_for_sheet(1)["status"], "paid")
        c.post(f"/api/payment/{p['id']}/status", json={"status": "prepared"})

    r = c.get(f"/ics/{ics_task['id']}")
    eq("H17 ics route 200", r.status_code, 200)
    eq("H18 ics content type", r.headers["Content-Type"].split(";")[0], "text/calendar")
    check("H19 ics attachment header", "attachment" in r.headers.get("Content-Disposition", ""))
    eq("H20 ics missing task 404", c.get("/ics/99999").status_code, 404)


# --------------------------------------------------------------------------- #
def main():
    seed()
    test_dates()
    test_spc()
    test_crosscheck()
    test_real_decode()
    test_extract()
    test_reminders()
    t = test_ics()
    test_board(t)
    print()
    if FAIL:
        print(f"FAILURES: {FAIL}  (pass {PASS})")
        sys.exit(1)
    print(f"ALL PASS  ({PASS} checks)")
    sys.exit(0)


if __name__ == "__main__":
    main()
