"""P3 — deadlines & reminders: analysis facts -> tasks; T-14/7/3/1 Telegram reminders; .ics export.

Deterministic layer (no LLM): deadline dates are parsed from the analyzer's
`deadline` facts; reminders are plain date math with an idempotent send log
(`reminders` table). The user can also add tasks by hand from the board.

Rules (keep stable):
- Reminder offsets T-14/7/3/1, fired on/after 09:00 local (a missed pass
  catches up on the next run); a passed-and-still-open deadline pings once.
- Messages state the REAL days left ("in 2 days" even if the 7-day slot was
  the one that fired late).
- Tasks on trashed batches are ignored everywhere.
- Backfilled tasks already overdue by more than LATE_SILENT_DAYS start silent
  (visible on the board, no ping) so a first-run backfill cannot spam.
"""
import json
import re
import socket
import time
from datetime import date, datetime, timedelta

from . import db
from .notify import send_telegram

REMIND_OFFSETS = [14, 7, 3, 1]      # days before due
OVERDUE_OFFSET = -1                  # one-time "deadline passed" notice
REMIND_HOUR = 9                      # local hour; passes before this are skipped
LATE_SILENT_DAYS = 7                 # backfilled older than this: no pings

MONTHS = {"januar": 1, "februar": 2, "märz": 3, "maerz": 3, "april": 4, "mai": 5,
          "juni": 6, "juli": 7, "august": 8, "september": 9, "oktober": 10,
          "november": 11, "dezember": 12}

TASK_STATUSES = ("open", "done", "cancelled")


# --------------------------------------------------------------------------- #
# dates
# --------------------------------------------------------------------------- #

def _safe_date(y: int, m: int, d: int):
    if not (2000 <= y <= 2100):
        return None
    try:
        return date(y, m, d)
    except ValueError:
        return None


def parse_date(text):
    """First plausible date in a printed string -> datetime.date or None.

    Handles 04.11.2026 | 4.11.2026 | 31. Juli 2026 | 2026-11-04.
    """
    t = str(text or "")
    m = re.search(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b", t)
    if m:
        return _safe_date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", t)
    if m:
        return _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(r"\b(\d{1,2})\.\s*([A-Za-zÄÖÜäöü]{3,})\s+(\d{4})\b", t)
    if m:
        mon = MONTHS.get(m.group(2).lower())
        if mon:
            return _safe_date(int(m.group(3)), mon, int(m.group(1)))
    return None


def fmt_date(d) -> str:
    return d.strftime("%d.%m.%Y") if d else ""


# --------------------------------------------------------------------------- #
# tasks
# --------------------------------------------------------------------------- #

def mail_title(con, sid):
    """First document title of the sheet's stored analysis (for messages/ics)."""
    row = con.execute("SELECT json FROM analyses WHERE sheet_id=? AND status='ok'",
                      (sid,)).fetchone()
    if row and row["json"]:
        try:
            docs = json.loads(row["json"]).get("documents") or []
            if docs:
                return docs[0].get("title") or None
        except (TypeError, json.JSONDecodeError):
            pass
    return None


def add_task(sid, title, due=None, kind="task", source="user", con=None):
    """Insert a task. `due` = ISO string / date / None. Returns the new id."""
    own = con is None
    if own:
        con = db.connect()
    try:
        if isinstance(due, date):
            due = due.isoformat()
        elif due:
            d = parse_date(due)
            if not d:
                raise ValueError(f"unparseable due date: {due!r}")
            due = d.isoformat()
        else:
            due = None
        cur = con.execute(
            "INSERT INTO tasks(sheet_id, title, kind, due, status, source, created_at) "
            "VALUES (?,?,?,?,'open',?,?)",
            (sid, (title or "").strip() or "task", kind, due, source, time.time()))
        if own:
            con.commit()
        return cur.lastrowid
    finally:
        if own:
            con.close()


def extract_for_sheet(sid, con=None):
    """Create tasks from the sheet's `deadline` facts (idempotent). Returns new ids."""
    own = con is None
    if own:
        con = db.connect()
    try:
        row = con.execute(
            "SELECT s.id FROM sheets s JOIN batches b ON b.id=s.batch_id "
            "WHERE s.id=? AND COALESCE(b.status,'')!='trashed'", (sid,)).fetchone()
        if not row:
            return []
        new = []
        today = date.today()
        for f in con.execute(
                "SELECT value FROM facts WHERE sheet_id=? AND kind='deadline' "
                "ORDER BY id", (sid,)):
            value = (f["value"] or "").strip()
            due = parse_date(value)
            if not due:
                continue
            dup = con.execute(
                "SELECT id FROM tasks WHERE sheet_id=? AND kind='deadline' "
                "AND title=? AND COALESCE(due,'')=?", (sid, value, due.isoformat())
            ).fetchone()
            if dup:
                continue
            cur = con.execute(
                "INSERT INTO tasks(sheet_id, title, kind, due, status, source, created_at) "
                "VALUES (?,?,'deadline',?,'open','analysis',?)",
                (sid, value, due.isoformat(), time.time()))
            tid = cur.lastrowid
            new.append(tid)
            if (today - due).days > LATE_SILENT_DAYS:
                # backfill of a long-past deadline: visible, but no ping storm
                now = time.time()
                for o in REMIND_OFFSETS + [OVERDUE_OFFSET]:
                    con.execute(
                        "INSERT OR IGNORE INTO reminders(task_id, offset_days, sent_at) "
                        "VALUES (?,?,?)", (tid, o, now))
        if own:
            con.commit()
        return new
    finally:
        if own:
            con.close()


def set_task_status(tid, status, con=None):
    if status not in TASK_STATUSES:
        raise ValueError(f"status must be one of {TASK_STATUSES}")
    own = con is None
    if own:
        con = db.connect()
    try:
        con.execute("UPDATE tasks SET status=?, done_at=? WHERE id=?",
                    (status, time.time() if status != "open" else None, tid))
        if own:
            con.commit()
    finally:
        if own:
            con.close()


# --------------------------------------------------------------------------- #
# reminders
# --------------------------------------------------------------------------- #

def _sent_offsets(con, tid):
    return {r["offset_days"] for r in con.execute(
        "SELECT offset_days FROM reminders WHERE task_id=? AND sent_at IS NOT NULL",
        (tid,))}


def _payment_line(con, sid):
    """'payment prepared: …' line for a reminder.

    Prefers a payment decoded on the task's own sheet; a mail's payment part
    often sits on a SIBLING sheet of the same feeding batch (invoice +
    Einzahlungsschein), so that is the fallback — with the source sheet noted
    so the pairing stays visible.
    """
    row = con.execute("SELECT sheet_id, data_json, status FROM payments "
                      "WHERE sheet_id=?", (sid,)).fetchone()
    if row is None:
        row = con.execute(
            "SELECT p.sheet_id, p.data_json, p.status FROM payments p "
            "JOIN sheets s2 ON s2.id=p.sheet_id "
            "WHERE s2.batch_id=(SELECT batch_id FROM sheets WHERE id=?) "
            "ORDER BY p.sheet_id LIMIT 1", (sid,)).fetchone()
    if not row or row["status"] != "prepared":
        return None
    try:
        d = json.loads(row["data_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return None
    if not d.get("amount"):
        return None
    line = f"payment prepared: {d.get('amount')} {d.get('currency') or 'CHF'} → " \
           f"{d.get('iban_fmt') or d.get('iban')}"
    ref = d.get("reference_fmt") or d.get("reference")
    if ref:
        line += f" · ref {ref}"
    if row["sheet_id"] != sid:
        line += f" (sheet {row['sheet_id']})"
    return line


def _overdue_text(n):
    return "1 day overdue" if n == 1 else f"{n} days overdue"


def build_message(con, t, d):
    """The exact Telegram text for one firing (kept separate => easy to QA)."""
    due = date.fromisoformat(t["due"])
    title = mail_title(con, t["sheet_id"]) or t["title"] or f"sheet {t['sheet_id']}"
    if d < 0:
        head = (f"BUTLER — deadline PASSED: was due {fmt_date(due)} "
                f"({_overdue_text(-d)})")
    else:
        when = "today" if d == 0 else ("tomorrow" if d == 1 else f"in {d} days")
        head = f"BUTLER — deadline {when}: {fmt_date(due)}"
    lines = [head, title]
    pay = _payment_line(con, t["sheet_id"])
    if pay:
        lines.append(pay)
    lines.append(f"http://{socket.gethostname()}.local:9500/sheet/{t['sheet_id']}")
    return "\n".join(lines)


def run_reminders(notify=True, dry=False, asof=None, verbose=False):
    """One pass over open, dated tasks. Returns the messages (sent or would-send).

    dry=True: build the messages but never send and never mark them sent.
    asof=<date>: pretend "today" (also disables the 09:00 hour gate — QA mode).
    A failed send is NOT marked -> retried on the next pass.
    """
    db.init()
    today = asof or date.today()
    now = datetime.now()
    if asof is None and now.hour < REMIND_HOUR:
        return []
    con = db.connect()
    out = []
    try:
        rows = con.execute(
            "SELECT t.id, t.sheet_id, t.title, t.due FROM tasks t "
            "JOIN sheets s ON s.id=t.sheet_id JOIN batches b ON b.id=s.batch_id "
            "WHERE t.status='open' AND t.due IS NOT NULL "
            "AND COALESCE(b.status,'')!='trashed' ORDER BY t.due, t.id").fetchall()
        for t in rows:
            due = date.fromisoformat(t["due"])
            d = (due - today).days
            sent = _sent_offsets(con, t["id"])
            if d < 0:
                pending = [] if OVERDUE_OFFSET in sent else [OVERDUE_OFFSET]
            else:
                pending = [o for o in REMIND_OFFSETS if d <= o and o not in sent]
            if not pending:
                continue
            text = build_message(con, t, d)
            if dry:
                out.append(text)
                continue
            if notify and not send_telegram(text):
                continue  # transient failure — retried next pass
            for o in pending:
                con.execute(
                    "INSERT OR REPLACE INTO reminders(task_id, offset_days, sent_at) "
                    "VALUES (?,?,?)", (t["id"], o, time.time()))
            con.commit()
            out.append(text)
            if verbose:
                print(f"[remind] task {t['id']} (d={d}): sent {pending}")
    finally:
        con.close()
    return out


# --------------------------------------------------------------------------- #
# .ics export
# --------------------------------------------------------------------------- #

def _ics_escape(s):
    return (str(s or "").replace("\\", "\\\\").replace(";", "\\;")
            .replace(",", "\\,").replace("\r", "").replace("\n", "\\n"))


def make_ics(t, mail_title=None):
    """All-day VEVENT (+1 day-before alarm) for one task row."""
    due = date.fromisoformat(t["due"])
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    desc = f"BUTLER task {t['id']} — sheet {t['sheet_id']}"
    if mail_title:
        desc = f"{mail_title} · {desc}"
    desc += f" · http://{socket.gethostname()}.local:9500/sheet/{t['sheet_id']}"
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0",
        "PRODID:-//BUTLER//mail triage//EN", "CALSCALE:GREGORIAN",
        "BEGIN:VEVENT",
        f"UID:butler-task-{t['id']}@{socket.gethostname()}",
        f"DTSTAMP:{stamp}",
        f"DTSTART;VALUE=DATE:{due.strftime('%Y%m%d')}",
        f"DTEND;VALUE=DATE:{(due + timedelta(days=1)).strftime('%Y%m%d')}",
        f"SUMMARY:{_ics_escape('Deadline: ' + (t['title'] or ''))}",
        f"DESCRIPTION:{_ics_escape(desc)}",
        "BEGIN:VALARM", "TRIGGER:-P1D", "ACTION:DISPLAY",
        f"DESCRIPTION:{_ics_escape('Deadline tomorrow')}", "END:VALARM",
        "END:VEVENT", "END:VCALENDAR",
    ]
    return "\r\n".join(lines) + "\r\n"


# --------------------------------------------------------------------------- #
# board display helpers
# --------------------------------------------------------------------------- #

def sheet_task_rows(sid, today=None):
    """Task rows decorated for the sheet page (due_fmt, reminder progress, ics)."""
    today = today or date.today()
    con = db.connect()
    try:
        rows = [dict(t) for t in con.execute(
            "SELECT * FROM tasks WHERE sheet_id=? ORDER BY (due IS NULL), due, id",
            (sid,))]
        sentmap = {}
        for r in con.execute(
                "SELECT task_id, offset_days FROM reminders WHERE sent_at IS NOT NULL"):
            sentmap.setdefault(r["task_id"], set()).add(r["offset_days"])
        for t in rows:
            sent = sentmap.get(t["id"], set())
            t["due_fmt"] = fmt_date(date.fromisoformat(t["due"])) if t["due"] else None
            t["ics"] = bool(t["due"] and t["status"] == "open")
            line = "—"
            if t["status"] == "open" and t["due"]:
                due = date.fromisoformat(t["due"])
                d = (due - today).days
                sent_txt = ",".join(str(o) for o in sorted(
                    (o for o in sent if o >= 0), reverse=True)) or "–"
                pend = [o for o in REMIND_OFFSETS if d <= o and o not in sent]
                if d < 0:
                    line = f"sent {sent_txt} · OVERDUE" if OVERDUE_OFFSET in sent \
                        else f"sent {sent_txt} · overdue — ping pending"
                elif pend:
                    nxt = min(pend)
                    line = f"sent {sent_txt} · next {nxt} → {fmt_date(due - timedelta(days=nxt))}"
                elif d > max(REMIND_OFFSETS):
                    nxt = max(REMIND_OFFSETS)
                    line = f"sent {sent_txt} · next {nxt} → {fmt_date(due - timedelta(days=nxt))}"
                else:
                    line = f"sent {sent_txt}"
            elif t["status"] == "done":
                line = "done"
            elif t["status"] == "cancelled":
                line = "cancelled"
            t["reminder_line"] = line
        return rows
    finally:
        con.close()


def open_summary():
    """(next_due_iso, n_open) across all non-trashed open tasks — board header."""
    con = db.connect()
    try:
        r = con.execute(
            "SELECT MIN(CASE WHEN t.due IS NOT NULL THEN t.due END) AS next_due, "
            "COUNT(*) AS n FROM tasks t JOIN sheets s ON s.id=t.sheet_id "
            "JOIN batches b ON b.id=s.batch_id "
            "WHERE t.status='open' AND COALESCE(b.status,'')!='trashed'").fetchone()
        return (r["next_due"], r["n"] or 0)
    finally:
        con.close()
