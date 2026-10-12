"""P3 — Swiss QR-bill decode: payment-part sheet -> structured payment data (PREP ONLY).

The QR code is decoded deterministically (zxing-cpp) from the sheet images —
the analyzer's vision facts are used only to CROSS-CHECK the result. Payment
data is stored in `payments` and shown on the board with copy-ready fields;
"paid" is a status the user flips after paying in their own banking. This
module never executes or schedules a payment.

Notes
- Swiss QR payloads ("SPC") are UTF-8, fields separated by CRLF (we tolerate LF).
- QR-IBANs (IID 30000-31999) legitimately differ from the invoice's printed
  IBAN for the same account, so the account cross-check compares the shared
  digit tail, not the full IBAN.
- Missing zxing-cpp or no QR found => return None (graceful; never fatal).
"""
import json
import pathlib
import re
import time

from . import db

QR_REF_TYPES = ("QRR", "SCOR", "NON")


def _digits(s) -> str:
    return re.sub(r"\D", "", str(s or ""))


def fmt_iban(s) -> str:
    s = re.sub(r"\s+", "", str(s or ""))
    return " ".join(s[i:i + 4] for i in range(0, len(s), 4))


def fmt_ref(ref_type, ref) -> str:
    r = re.sub(r"\s+", "", str(ref or ""))
    if not r:
        return ""
    if ref_type == "QRR" and r.isdigit() and len(r) == 27:
        return " ".join([r[:2]] + [r[2 + 5 * i: 7 + 5 * i] for i in range(5)])
    if ref_type == "SCOR":
        return " ".join(r[i:i + 4] for i in range(0, len(r), 4))
    return r


def _money(v):
    try:
        return round(float(re.sub(r"[^\d.]", "", str(v).replace("'", "").replace(",", "."))), 2)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# decode
# --------------------------------------------------------------------------- #

def _decode_qr(paths):
    """First 'SPC' (Swiss QR) payload across the side images, or None."""
    try:
        import zxingcpp
        import numpy as np
        from PIL import Image
    except ImportError:
        return None
    for p in paths:
        try:
            img = Image.open(p)
            arr = np.array(img.convert("L"))
            res = zxingcpp.read_barcodes(arr)
        except Exception:
            continue
        for r in res:
            text = (r.text or "")
            if text.startswith("SPC") and re.match(r"^SPC[\r\n]", text):
                return text
    return None


def parse_spc(text):
    """Swiss QR payload -> dict (tolerates missing trailing fields)."""
    fields = re.split(r"\r\n|\n|\r", (text or "").strip())

    def g(i):
        return fields[i].strip() if i < len(fields) else ""

    def addr(base):
        return {"type": g(base), "name": g(base + 1), "street": g(base + 2),
                "house": g(base + 3), "zip": g(base + 4), "town": g(base + 5),
                "country": g(base + 6)}

    return {
        "qr_type": g(0), "version": g(1), "coding": g(2), "iban": g(3),
        "creditor": addr(4), "amount": g(18), "currency": g(19),
        "ultimate_creditor": addr(11), "debtor": addr(20),
        "ref_type": g(27), "reference": g(28), "message": g(29),
        "trailer": g(30),
    }


def crosscheck(data, facts):
    """Compare decoded data against the analyzer's facts.

    facts: iterable of {"kind","value"}. Returns {"amount":bool|None,
    "iban":bool|None, "reference":bool|None, "notes":[str]} — None = not
    comparable (no matching fact present)."""
    amount_v = [f["value"] for f in facts if f["kind"] == "amount_due"]
    account_v = [f["value"] for f in facts if f["kind"] == "account"]
    ref_v = [f["value"] for f in facts
             if f["kind"] in ("reference", "account", "subject", "other")]
    checks, notes = {}, []

    amt = _money(data.get("amount"))
    c_amts = [_money(v) for v in amount_v]
    c_amts = [a for a in c_amts if a is not None]
    checks["amount"] = None if (amt is None or not c_amts) else (amt in c_amts)

    iban_d = _digits(data.get("iban"))
    tails = {_digits(v)[-8:] for v in account_v if len(_digits(v)) >= 8}
    if not iban_d or not tails:
        checks["iban"] = None
    else:
        checks["iban"] = iban_d[-8:] in tails
        if checks["iban"] and not any(iban_d and _digits(v) == iban_d for v in account_v):
            notes.append("QR-IBAN differs from the printed IBAN — account tail matches")

    ref_d = _digits(data.get("reference"))
    if not ref_d or not ref_v:
        checks["reference"] = None
    else:
        checks["reference"] = any(
            len(_digits(v)) >= 8 and (_digits(v) in ref_d or ref_d in _digits(v))
            for v in ref_v)
    if checks["reference"] is False:
        notes.append("reference not found among the analysis facts")
    if checks["amount"] is False:
        notes.append("amount differs from the analysis facts")
    if checks["iban"] is False:
        notes.append("account tail not found among the analysis facts")
    return {"amount": checks["amount"], "iban": checks["iban"],
            "reference": checks["reference"], "notes": notes}


# --------------------------------------------------------------------------- #
# storage
# --------------------------------------------------------------------------- #

def decode_for_sheet(sid, verbose=False, con=None):
    """Decode + store the payment data for one sheet. Returns the row dict or None."""
    own = con is None
    if own:
        con = db.connect()
    try:
        s = con.execute(
            "SELECT s.*, b.folder, COALESCE(b.status,'') AS bstatus "
            "FROM sheets s JOIN batches b ON b.id=s.batch_id WHERE s.id=?",
            (sid,)).fetchone()
        if not s or s["bstatus"] == "trashed":
            return None
        paths = [pathlib.Path(s["folder"]) / p for p in (s["front"], s["back"]) if p]
        paths = [p for p in paths if p.exists()]
        payload = _decode_qr(paths)
        if not payload:
            if verbose:
                print(f"[qrpay] sheet {sid}: no Swiss QR found")
            return None
        data = parse_spc(payload)
        data["iban_fmt"] = fmt_iban(data["iban"])
        data["reference_fmt"] = fmt_ref(data["ref_type"], data["reference"])
        facts = [dict(r) for r in con.execute(
            "SELECT kind, value FROM facts WHERE sheet_id=?", (sid,))]
        check = crosscheck(data, facts)
        existing = con.execute("SELECT id, status FROM payments WHERE sheet_id=?",
                               (sid,)).fetchone()
        if existing:
            con.execute(
                "UPDATE payments SET ts=?, payload=?, data_json=?, check_json=? WHERE id=?",
                (time.time(), payload, json.dumps(data, ensure_ascii=False),
                 json.dumps(check, ensure_ascii=False), existing["id"]))
            pid, status = existing["id"], existing["status"]
        else:
            cur = con.execute(
                "INSERT INTO payments(sheet_id, ts, payload, data_json, check_json, "
                "status) VALUES (?,?,?,?,?,'prepared')",
                (sid, time.time(), payload, json.dumps(data, ensure_ascii=False),
                 json.dumps(check, ensure_ascii=False)))
            pid, status = cur.lastrowid, "prepared"
        if own:
            con.commit()
        if verbose:
            print(f"[qrpay] sheet {sid}: {data.get('amount')} {data.get('currency')} "
                  f"→ {data['iban_fmt']} (ref {data['reference_fmt'] or '-'}) "
                  f"checks={check['amount']}/{check['iban']}/{check['reference']}")
        return {"id": pid, "status": status, "data": data, "check": check}
    finally:
        if own:
            con.close()


def set_payment_status(pid, status, con=None):
    if status not in ("prepared", "paid"):
        raise ValueError("status must be prepared|paid")
    own = con is None
    if own:
        con = db.connect()
    try:
        con.execute("UPDATE payments SET status=?, status_ts=? WHERE id=?",
                    (status, time.time(), pid))
        if own:
            con.commit()
    finally:
        if own:
            con.close()


def payment_for_sheet(sid):
    """Parsed payment dict for display, or None."""
    con = db.connect()
    try:
        r = con.execute("SELECT * FROM payments WHERE sheet_id=?", (sid,)).fetchone()
    finally:
        con.close()
    if not r:
        return None
    try:
        data = json.loads(r["data_json"] or "{}")
        check = json.loads(r["check_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        data, check = {}, {}
    c = {k: check.get(k) for k in ("amount", "iban", "reference")}
    return {"id": r["id"], "status": r["status"], "data": data, "check": check,
            "check_ok": all(v is not False for v in c.values()),
            "check_marks": {k: ("ok" if v is True else ("mismatch" if v is False else "n/a"))
                            for k, v in c.items()},
            "copy_text": copy_text(data)}


def copy_text(data) -> str:
    """Copy-ready payment block (the user's banking-app style)."""
    c = data.get("creditor") or {}
    d = data.get("debtor") or {}
    lines = []
    if c.get("name"):
        addr = " ".join(x for x in (c.get("street"), c.get("house")) if x)
        lines.append("Recipient: " + ", ".join(
            x for x in (c.get("name"), addr,
                        " ".join(x for x in (c.get("zip"), c.get("town")) if x)) if x))
    lines.append(f"QR-IBAN: {data.get('iban_fmt') or data.get('iban')}")
    if data.get("amount"):
        lines.append(f"Amount: {data.get('amount')} {data.get('currency') or 'CHF'}")
    ref = data.get("reference_fmt") or data.get("reference")
    if ref:
        lines.append(f"Reference: {ref}"
                     + (f" ({data.get('ref_type')})" if data.get("ref_type") not in (None, "", "NON") else ""))
    if d.get("name"):
        addr = " ".join(x for x in (d.get("street"), d.get("house")) if x)
        lines.append("Debtor: " + ", ".join(
            x for x in (d.get("name"), addr,
                        " ".join(x for x in (d.get("zip"), d.get("town")) if x)) if x))
    return "\n".join(lines)
