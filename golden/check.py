#!/usr/bin/env python3
"""Golden-set checker: compare stored analyses against golden/golden.json.

Usage:  python3 golden/check.py            (from ~/BUTLER)
Exit code 0 = all pass, 1 = failures.
"""
import json
import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DB = ROOT / "var" / "butler.db"
GOLD = pathlib.Path(__file__).resolve().parent / "golden.json"


def load_stored(sheet_id):
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(
            "SELECT a.json, a.status FROM analyses a WHERE a.sheet_id=?", (sheet_id,)
        ).fetchone()
    finally:
        con.close()


def sheet_ids_for_batch(con, batch_key):
    return [r["id"] for r in con.execute(
        "SELECT s.id FROM sheets s JOIN batches b ON b.id=s.batch_id "
        "WHERE b.key=? ORDER BY s.sheet_no", (batch_key,))]


def check_sheet(expect, sheet_id):
    """Returns list of (check_name, ok, detail)."""
    out = []
    row = load_stored(sheet_id)
    if not row or row["status"] != "ok" or not row["json"]:
        out.append(("analysis", False, "no stored OK analysis"))
        return out
    a = json.loads(row["json"])
    docs = a.get("documents") or []
    out.append(("doc_count", len(docs) == expect.get("expect_docs", len(docs)),
                f"got {len(docs)}, expected {expect.get('expect_docs')}"))
    blob = json.dumps(a, ensure_ascii=False)
    cats = [d.get("category") for d in docs]
    if "category" in expect:
        out.append(("category", expect["category"] in cats,
                    f"got {cats}, expected {expect['category']}"))
    if "priority_in" in expect:
        pris = [d.get("priority") for d in docs]
        out.append(("priority", all(p in expect["priority_in"] for p in pris),
                    f"got {pris}, allowed {expect['priority_in']}"))
    for needle in expect.get("facts_contains", []):
        out.append((f"fact:{needle}", needle in blob, "found" if needle in blob else "MISSING"))
    words = expect.get("notes_contains_any", [])
    if words:
        ok = any(w in blob for w in words)
        out.append(("notes", ok, f"any of {words}" if not ok else "found"))
    if "deadline" in expect:
        dls = [f.get("value") for d in docs for f in (d.get("facts") or [])
               if f.get("kind") == "deadline"]
        ok = any(expect["deadline"] in (x or "") for x in dls)
        out.append(("deadline", ok, f"got {dls}"))
    return out


def main():
    gold = json.loads(GOLD.read_text())
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    ids = sheet_ids_for_batch(con, gold["batch_key"])
    con.close()
    if not ids:
        print(f"batch {gold['batch_key']} not in registry")
        return 1
    by_no = {str(i + 1): sid for i, sid in enumerate(ids)}
    failures = 0
    for no, expect in gold["sheets"].items():
        sid = by_no.get(no)
        print(f"— sheet {no} (db id {sid}) —")
        for name, ok, detail in check_sheet(expect, sid):
            mark = "PASS" if ok else "FAIL"
            print(f"  [{mark}] {name}: {detail}")
            if not ok:
                failures += 1
    print()
    print(f"golden: {'ALL PASS' if failures == 0 else f'{failures} FAILURE(S)'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
