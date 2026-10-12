"""P2 — DeepSeek vision analysis: sheet → documents → cited facts.

Reads each scanned sheet (front [+ back]) as one or more mail "documents",
stores a cleaned analysis JSON + flat facts (with citations), moves the sheet
inbox → analyzing → review (or noise), and sends one Telegram ping per
document ("each mail is special").

P3 enrichment runs right after a successful analysis (deterministic, guarded):
Swiss QR payment decode (qrpay) + deadline facts → tasks (tasks).

Provider-agnostic: the model call lives behind llm.analyze_images().
"""
import datetime
import json
import os
import pathlib
import re
import socket
import time

from . import db, llm
from .notify import send_telegram

VALID_CATEGORIES = {"bill", "official", "insurance", "bank", "employer",
                    "contract", "personal", "advert", "other"}
VALID_DISPOSITIONS = {"urgent-reply", "reply", "action", "waiting", "reference", "noise"}
VALID_PRIORITIES = {"P0", "P1", "P2", "P3"}
FACT_KINDS = {"amount_due", "amount_paid", "salary", "deadline", "document_date",
              "reference", "account", "case_number", "subject", "other"}
MAX_ATTEMPTS = 3

# Analysis start gates (user decision 2026-10-12 night): a new letter waits in
# INBOX for at least `hold_minutes` after registration AND until the next `at`
# clock time — then the analyzer picks it up. "analyze now" lifts both gates
# (batches.released). Config: ~/.config/butler/analyze.json
# {"hold_minutes": 60, "at": ["22:00"]}; env overrides BUTLER_ANALYZE_HOLD /
# BUTLER_ANALYZE_AT (comma list; empty string disables the schedule gate).
GATE_FILE = pathlib.Path.home() / ".config/butler/analyze.json"


def gate_cfg() -> dict:
    hold, at = 60, ["22:00"]
    try:
        raw = json.loads(GATE_FILE.read_text())
        if raw.get("hold_minutes") is not None:
            hold = max(0, int(raw["hold_minutes"]))
        if raw.get("at") is not None:
            at = [str(x).strip() for x in raw["at"] if str(x).strip()]
    except Exception:
        pass
    if os.environ.get("BUTLER_ANALYZE_HOLD") not in (None, ""):
        hold = max(0, int(os.environ["BUTLER_ANALYZE_HOLD"]))
    if "BUTLER_ANALYZE_AT" in os.environ:
        at = [x.strip() for x in os.environ["BUTLER_ANALYZE_AT"].split(",") if x.strip()]
    return {"hold_minutes": hold, "at": at}


def _next_slot(ts: float, times):
    """First occurrence of any 'HH:MM' strictly after ts (local time), or None."""
    if not times:
        return None
    base = datetime.datetime.fromtimestamp(ts)
    best = None
    for t in times:
        try:
            hh, mm = (int(x) for x in t.split(":", 1))
            cand = base.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if cand <= base:
                cand += datetime.timedelta(days=1)
        except Exception:
            continue
        if best is None or cand < best:
            best = cand
    return best.timestamp() if best else None


def release_ts(created_at: float, cfg=None) -> float:
    """When this batch may be analyzed: registration + hold, and not before the
    next scheduled clock time."""
    cfg = cfg or gate_cfg()
    ts = float(created_at or 0) + cfg["hold_minutes"] * 60
    slot = _next_slot(float(created_at or 0), cfg["at"])
    if slot is not None:
        ts = max(ts, slot)
    return ts

SYSTEM = (
    "You are the mail analyst inside BUTLER, a private mail-triage system for one "
    "user in Switzerland (canton Thurgau). You receive the FRONT and BACK images of "
    "ONE physically scanned A4 sheet from a duplex ADF scanner. The two sides may be "
    "two pages of one document, parts of two different documents, or one side may be "
    "nearly blank.\n"
    "Rules:\n"
    "- Read carefully. Mail is usually German (Swiss), sometimes English or Russian.\n"
    "- Write the summary in English (if the document itself is in Russian, summarize in Russian). Keep titles and names as printed.\n"
    "- Identify each distinct mail piece (document) on the sheet and analyze it.\n"
    "- Every fact carries a citation: side + location (e.g. front:top-right).\n"
    "- Never invent values that are not printed. Unreadable/missing -> null and set needs_review.\n"
    "- Copy amounts, dates, references EXACTLY as printed (keep CHF 1'234.56 style).\n"
    "- Facts: include every significant printed line item — each deduction, subtotal and total, every date/deadline, references, account and case numbers.\n"
    "- Handwritten notes or sketches on a side: describe them in content_notes/sheet_notes; "
    "make a document for them only if they clearly are their own meaningful piece.\n"
    "- Output STRICT JSON only - no markdown fences, no commentary."
)

PROMPT = """Sheet: {paper}, {dpi} dpi. Sides present: {sides} (images supplied in that order).
Analyze the mail on this sheet and reply with STRICT JSON:
{{
  "documents": [
    {{
      "title": "short title, e.g. Lohnabrechnung September 2026",
      "sender": "organization/person or null",
      "recipient": "person or null",
      "language": "de|en|ru|other",
      "category": "bill|official|insurance|bank|employer|contract|personal|advert|other",
      "disposition": "urgent-reply|reply|action|waiting|reference|noise",
      "priority": "P0|P1|P2|P3",
      "summary": "2-3 plain sentences",
      "sides": ["front", "back"],
      "content_notes": "what each side shows, incl. sketches/handwriting/blank",
      "facts": [
        {{"kind": "amount_due|amount_paid|salary|deadline|document_date|reference|account|case_number|subject|other",
          "value": "exactly as printed", "citation": "front:top-right", "confidence": 0.9}}
      ],
      "needs_review": false,
      "review_reason": null
    }}
  ],
  "sheet_notes": "anything else (blank side, sketches, notes)"
}}
Priority guide: P0 = legal/collection (Betreibung, Mahnung with Frist, Vorladung, Kuendigung);
P1 = money or a deadline; P2 = a reply is expected; P3 = file/reference only.
Clear adverts -> disposition "noise", priority P3.
A blank back side: mention it in sheet_notes, do not invent a document for it."""


def _img_path(folder, name):
    if not name:
        return None
    return pathlib.Path(folder) / name


def _paper_info(folder):
    try:
        m = json.loads((pathlib.Path(folder) / "manifest.json").read_text())
        return m.get("paper", "A4"), m.get("dpi", 300)
    except Exception:
        return "A4", 300


def _parse(text):
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-z]*\s*", "", t, flags=re.I)
        t = re.sub(r"```\s*$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", t, re.S)
        if not m:
            raise
        return json.loads(m.group(0))


def _clean(obj):
    """Tolerant normalization of the model's JSON."""
    docs = []
    for d in ((obj or {}).get("documents") or []):
        if not isinstance(d, dict):
            continue
        cat = str(d.get("category") or "other").lower()
        disp = str(d.get("disposition") or "reference").lower()
        pri = str(d.get("priority") or "P3").upper()
        facts = []
        for f in (d.get("facts") or []):
            if not isinstance(f, dict):
                continue
            v = f.get("value")
            if v is None or v == "":
                continue
            v = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
            try:
                conf = max(0.0, min(1.0, float(f.get("confidence"))))
            except (TypeError, ValueError):
                conf = None
            facts.append({
                "kind": f.get("kind") if f.get("kind") in FACT_KINDS else "other",
                "value": v,
                "citation": str(f.get("citation") or ""),
                "confidence": conf,
            })
        docs.append({
            "title": str(d.get("title") or "Untitled"),
            "sender": d.get("sender") or None,
            "recipient": d.get("recipient") or None,
            "language": str(d.get("language") or "other").lower(),
            "category": cat if cat in VALID_CATEGORIES else "other",
            "disposition": disp if disp in VALID_DISPOSITIONS else "reference",
            "priority": pri if pri in VALID_PRIORITIES else "P3",
            "summary": str(d.get("summary") or ""),
            "sides": [str(x) for x in (d.get("sides") or [])],
            "content_notes": str(d.get("content_notes") or ""),
            "facts": facts,
            "needs_review": bool(d.get("needs_review")),
            "review_reason": d.get("review_reason") or None,
        })
    return {"documents": docs, "sheet_notes": str((obj or {}).get("sheet_notes") or "")}


def _fail(con, sid, err, model, raw=None):
    """Record a failed attempt (keeps status in inbox for a bounded retry)."""
    row = con.execute("SELECT attempts FROM analyses WHERE sheet_id=?", (sid,)).fetchone()
    att = (row["attempts"] if row else 0) + 1
    con.execute(
        "INSERT INTO analyses(sheet_id, ts, model, status, error, json, raw, attempts) "
        "VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(sheet_id) DO UPDATE SET ts=excluded.ts, model=excluded.model, "
        "status='error', error=excluded.error, json=excluded.json, raw=excluded.raw, "
        "attempts=excluded.attempts",
        (sid, time.time(), model, "error", str(err)[:500], None, raw, att))
    con.execute("UPDATE sheets SET status='inbox' WHERE id=?", (sid,))
    con.commit()
    return att


def analyze_sheet(sid, notify=True, verbose=False):
    db.init()
    con = db.connect()
    try:
        s = con.execute(
            "SELECT s.*, b.folder, b.key AS batch_key, b.ts AS batch_ts "
            "FROM sheets s JOIN batches b ON b.id=s.batch_id WHERE s.id=?", (sid,)
        ).fetchone()
        if not s:
            raise ValueError(f"sheet {sid} not found")

        # Which sides are part of the letter? (removed sides are skipped)
        paths, sides = [], []
        for side, rel in (("front", s["front"]), ("back", s["back"])):
            if not rel or s[f"{side}_excluded"]:
                continue
            p = _img_path(s["folder"], rel)
            if p and p.exists():
                paths.append(p)
                sides.append(side)
        if not paths:
            included = [1 for side, rel in (("front", s["front"]), ("back", s["back"]))
                        if rel and not s[f"{side}_excluded"]]
            if not included:  # every side was removed from the letter — nothing to read
                return {"sheet": sid, "status": "skipped",
                        "reason": "all sides removed from the letter"}
            raise llm.LLMError("no image files found")

        con.execute("UPDATE sheets SET status='analyzing' WHERE id=?", (sid,))
        con.commit()

        paper, dpi = _paper_info(s["folder"])
        prompt = PROMPT.format(paper=paper, dpi=dpi, sides=", ".join(sides))

        try:
            res = llm.analyze_images(prompt, paths, system=SYSTEM)
        except llm.LLMError as e:
            att = _fail(con, sid, e, llm.model_label())
            if att >= MAX_ATTEMPTS and notify:
                send_telegram(f"BUTLER: analysis FAILED for sheet {sid} "
                              f"({att} attempts): {e}")
            if verbose:
                print(f"[analyze] sheet {sid}: ERROR {e} (attempt {att})")
            return {"sheet": sid, "status": "error", "error": str(e), "attempts": att}

        raw = res["text"]
        try:
            cleaned = _clean(_parse(raw))
        except Exception as e:
            att = _fail(con, sid, f"bad JSON: {e}", res["model"], raw)
            if att >= MAX_ATTEMPTS and notify:
                send_telegram(f"BUTLER: analysis reply unparseable for sheet {sid} "
                              f"({att} attempts)")
            if verbose:
                print(f"[analyze] sheet {sid}: BAD JSON ({e}) attempt {att}")
            return {"sheet": sid, "status": "error", "error": f"bad JSON: {e}",
                    "attempts": att}

        docs = cleaned["documents"]
        new_status = ("noise" if docs and all(d["disposition"] == "noise" for d in docs)
                      else "review")
        con.execute("DELETE FROM analyses WHERE sheet_id=?", (sid,))
        con.execute(
            "INSERT INTO analyses(sheet_id, ts, model, status, json, raw, attempts) "
            "VALUES (?,?,?,?,?,?,0)",
            (sid, time.time(), res["model"], "ok",
             json.dumps(cleaned, ensure_ascii=False), raw))
        con.execute("DELETE FROM facts WHERE sheet_id=?", (sid,))
        for di, d in enumerate(docs):
            for f in d["facts"]:
                con.execute(
                    "INSERT INTO facts(sheet_id, doc_idx, kind, value, citation, confidence) "
                    "VALUES (?,?,?,?,?,?)",
                    (sid, di, f["kind"], f["value"], f["citation"], f["confidence"]))
        con.execute("UPDATE sheets SET status=? WHERE id=?", (new_status, sid))
        # P3 enrichment — deterministic, never fatal to the analysis itself:
        # Swiss QR payment decode + deadline facts -> tasks (reminders follow).
        try:
            from . import qrpay, tasks
            qrpay.decode_for_sheet(sid, con=con)
            tasks.extract_for_sheet(sid, con=con)
        except Exception as e:
            print(f"[analyze] enrich sheet {sid}: {type(e).__name__}: {e}")
        con.commit()
    finally:
        con.close()

    notified = 0
    if notify:
        for i, d in enumerate(docs, 1):
            if d["disposition"] == "noise":   # card yes, ping no
                continue
            if _notify_doc(sid, d, i, len(docs)):
                notified += 1
    if verbose:
        print(f"[analyze] sheet {sid}: {len(docs)} document(s) -> {new_status}, "
              f"{notified} notification(s), model={res['model']}, usage={res.get('usage')}")
    return {"sheet": sid, "status": new_status, "docs": len(docs), "notified": notified,
            "model": res["model"], "usage": res.get("usage", {})}


def _notify_doc(sid, d, idx, total):
    hot_kinds = ("deadline", "amount_due", "salary", "amount_paid", "case_number")
    hot = [f for f in d["facts"] if f["kind"] in hot_kinds][:4] or d["facts"][:3]
    head = "BUTLER - new mail" + (f" ({idx}/{total})" if total > 1 else "")
    lines = [head, d["title"],
             f"{d['sender'] or '-'} | {d['category']} | {d['priority']}"
             + (" | NEEDS REVIEW" if d["needs_review"] else ""),
             d["summary"] or "(no summary)"]
    for f in hot:
        lines.append(f"- {f['kind']}: {f['value']}")
    lines.append(f"http://{socket.gethostname()}.local:9500/sheet/{sid}")
    return send_telegram("\n".join(lines))


def pending_sheet_ids(force=False):
    """Sheets ready for analysis. Release gates (hold + schedule) apply unless
    force=True or the batch was released via 'analyze now' (batches.released)."""
    con = db.connect()
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT s.id, b.created_at AS bcreated, "
            "COALESCE(b.released,0) AS breleased "
            "FROM sheets s JOIN batches b ON b.id=s.batch_id "
            "LEFT JOIN analyses a ON a.sheet_id=s.id "
            "WHERE s.status IN ('inbox','analyzing') "
            "AND COALESCE(b.status,'')!='trashed' "
            "AND COALESCE(s.excluded,0)=0 "
            "AND (COALESCE(s.front_excluded,0)=0 OR COALESCE(s.back_excluded,0)=0) "
            "AND (a.id IS NULL OR (a.status='error' AND a.attempts < ?)) "
            "ORDER BY s.id", (MAX_ATTEMPTS,))]
    finally:
        con.close()
    if force:
        return [r["id"] for r in rows]
    cfg = gate_cfg()
    now = time.time()
    return [r["id"] for r in rows
            if r["breleased"] or release_ts(r["bcreated"], cfg) <= now]


def run_pending(notify=True, verbose=False, force=False):
    db.init()
    done = []
    for sid in pending_sheet_ids(force=force):
        try:
            r = analyze_sheet(sid, notify=notify, verbose=verbose)
            done.append(sid if r.get("status") != "error" else f"{sid}:error")
        except Exception as e:
            print(f"[analyze] sheet {sid} unexpected: {type(e).__name__}: {e}")
    return done
