"""Coverage hints — face identity (re-scan detection) + page-coverage checks.

The coverage/dedupe roadmap layer (PLAN §roadmap). Two deterministic,
best-effort hints per letter:

  1. duplicates — every kept side of a letter is fingerprinted with a
     rotation-tolerant dHash (16x16 = 256 bits per orientation, all four
     of 0/90/180/270 stored — sheets get fed upside-down or jammed sideways
     in practice). A side matches another side when the min-over-orientations
     Hamming distance is <= MATCH_BITS. Letters sharing >= MIN_MATCH_SIDES
     sides are reported as possible duplicates (re-scans), trashed targets
     included and marked. Side-agnostic: a sheet fed flipped has its two
     faces in swapped front/back slots across captures — that must still
     match (verified on real captures).

     Thresholds measured on the real mail corpus (2026-10-13, 46 sides,
     9 batches): the same page across independent captures scores 0-20
     (incl. upside-down and 90-degree jam captures), different pages score
     >= 53. MATCH_BITS = 32 sits in the empty band. Near-empty sides
     (< BLANK_MAX_DARK inked pixels — blank backs) carry no identity and
     are excluded from matching; measured blanks are 0.00-0.01% inked,
     text pages >= 0.39%.

  2. pages — page numbers printed on the letter ("Seite 2/4", "Page 1 of 3")
     are collected from the letter's stored analyses (cited facts + the raw
     model reply), merged per page total across bilingual variants
     (Seite/Page), and pages of a scheme that are not found in the letter
     are reported as possibly missing. Word-anchored numbers only — bare
     production folios ("20954 1/3") turned out too lossy to police (small
     print is often not extracted, and they may count sheets rather than
     pages: measured on the real corpus). The wording is deliberately "not
     found" — the analyzer can miss small print, so this is a prompt to
     look, not a verdict.

Faces are computed once per side and cached in `faces`; scan files are only
ever read. Everything here degrades silently: an unreadable image is
skipped, and hint errors never propagate to the caller (they are hints).
"""
import os
import pathlib
import re
import time

from . import db

SCANS_ROOT = pathlib.Path(os.environ.get(
    "BUTLER_SCANS_DIR", str(pathlib.Path.home() / "scans")))

_HKEYS = ("h0", "h90", "h180", "h270")
MATCH_BITS = 32          # min-over-orientations Hamming distance (see module doc)
BLANK_MAX_DARK = 0.0015  # inked-pixel fraction below which a side is "blank"
MIN_MATCH_SIDES = 2      # sides of THIS letter that must match to flag a dup
MAX_TOTAL = 12           # sanity cap for a folio scheme's page total

_WORD_FOLIO = re.compile(
    r"(?i)\b(seiten|seite|pages?|pag\.?|blatt|folio)\s*\.?\s*(\d{1,2})"
    r"\s*(?:/\s*|\s+von\s+)(\d{1,2})\b")
_WORD_LABEL = {"seite": "Seite", "seiten": "Seite", "page": "Page",
               "pages": "Page", "pag": "Page", "blatt": "Blatt", "folio": "Folio"}
_LABEL_PREF = ("Seite", "Page", "Blatt", "Folio")


# --------------------------------------------------------------------------- #
# hashing
# --------------------------------------------------------------------------- #

def _dhash_small(small):
    """16x16 dHash over a 17x16 grayscale image -> 256-bit int."""
    px = list(small.getdata())
    bits = 0
    for y in range(16):
        row = px[y * 17:(y + 1) * 17]
        for x in range(16):
            bits = (bits << 1) | (1 if row[x] > row[x + 1] else 0)
    return bits


def _hash_file(path):
    """(orientation hashes, blank) for one side image; None when unreadable.

    Orientation k = the draft image rotated by k degrees before hashing, so a
    rotated capture matches an upright one (calibrated on real captures).
    """
    try:
        from PIL import Image
        im = Image.open(path)
        im.draft("L", (400, 400))
        im = im.convert("L")
        px = list(im.getdata())
        n = len(px)
        if not n:
            return None
        dark = sum(1 for v in px if v < 96) / n
        hs = {}
        for k in (0, 90, 180, 270):
            small = im.rotate(k, expand=True).resize((17, 16), Image.LANCZOS)
            hs[f"h{k}"] = f"{_dhash_small(small):064x}"
        return hs, (dark <= BLANK_MAX_DARK)
    except Exception:
        return None


def _resolve(folder, rel):
    """Live path of a sheet side; falls back to the archive (trashed letters)."""
    p = pathlib.Path(folder) / rel
    if p.exists():
        return p
    arch = SCANS_ROOT / "archive"
    for cand in (arch / pathlib.Path(folder).name / rel, arch / rel):
        if cand.exists():
            return cand
    return None


def index_batch(bid, con=None, rebuild=False):
    """(Re)build cached face hashes for a batch's kept sides. Returns rows written.

    Kept sides only (user-removed scans/sides are not part of the letter);
    stale rows for sides that left the letter are dropped. Best-effort:
    unreadable files are skipped, nothing raises into the caller.
    """
    own = con is None
    if own:
        con = db.connect()
    try:
        b = con.execute("SELECT id, folder FROM batches WHERE id=?", (bid,)).fetchone()
        if not b:
            return 0
        kept = []
        for s in con.execute(
                "SELECT id, front, back, excluded, front_excluded, back_excluded "
                "FROM sheets WHERE batch_id=? ORDER BY sheet_no, id", (bid,)):
            if s["excluded"]:
                continue
            for side in ("front", "back"):
                rel = s[side]
                if rel and not s[side + "_excluded"]:
                    kept.append((s["id"], side, rel))
        if rebuild:
            con.execute("DELETE FROM faces WHERE batch_id=?", (bid,))
        have = {(r["sheet_id"], r["side"]) for r in con.execute(
            "SELECT sheet_id, side FROM faces WHERE batch_id=?", (bid,))}
        kept_keys = {(sid, side) for sid, side, _ in kept}
        for key in have - kept_keys:  # side left the letter — drop its face
            con.execute("DELETE FROM faces WHERE sheet_id=? AND side=?", key)
        n = 0
        for sid, side, rel in kept:
            if (sid, side) in have:
                continue
            p = _resolve(b["folder"], rel)
            if not p:
                continue
            res = _hash_file(p)
            if not res:
                continue
            hs, blank = res
            con.execute(
                "INSERT OR REPLACE INTO faces(sheet_id, batch_id, side, h0, h90, "
                "h180, h270, blank, ts) VALUES (?,?,?,?,?,?,?,?,?)",
                (sid, bid, side, hs["h0"], hs["h90"], hs["h180"], hs["h270"],
                 int(blank), time.time()))
            n += 1
        con.commit()
        return n
    except Exception as e:
        print(f"[coverage] index batch {bid}: {type(e).__name__}: {e}")
        return 0
    finally:
        if own:
            con.close()


# --------------------------------------------------------------------------- #
# duplicate hints
# --------------------------------------------------------------------------- #

def _minbits(a, b):
    """Min Hamming distance over all orientation pairs."""
    return min((a[x] ^ b[y]).bit_count() for x in _HKEYS for y in _HKEYS)


def _load_batch_faces(con, bid):
    return [{k: int(r[k], 16) for k in _HKEYS} for r in con.execute(
        "SELECT h0, h90, h180, h270 FROM faces WHERE batch_id=? AND blank=0",
        (bid,))]


def duplicate_hints(con, bid):
    """Letters whose sides overlap this letter's. [] when nothing to say."""
    mine = _load_batch_faces(con, bid)
    if len(mine) < MIN_MATCH_SIDES:
        return []
    theirs = {}
    for r in con.execute(
            "SELECT f.batch_id AS oid, b.status AS bstatus, b.key AS bkey, "
            "f.h0, f.h90, f.h180, f.h270 FROM faces f "
            "JOIN batches b ON b.id=f.batch_id "
            "WHERE f.batch_id != ? AND f.blank=0 AND f.batch_id IS NOT NULL", (bid,)):
        d = theirs.setdefault(r["oid"], {"status": r["bstatus"], "key": r["bkey"],
                                         "faces": []})
        d["faces"].append({k: int(r[k], 16) for k in _HKEYS})
    out = []
    for oid, d in theirs.items():
        matched = sum(1 for m in mine
                      if any(_minbits(m, t) <= MATCH_BITS for t in d["faces"]))
        if matched >= MIN_MATCH_SIDES:
            out.append({"other": oid, "matched": matched, "mine": len(mine),
                        "trashed": (d["status"] == "trashed"), "other_key": d["key"]})
    out.sort(key=lambda h: (h["trashed"], -h["matched"], -h["other"]))
    return out[:3]


# --------------------------------------------------------------------------- #
# page-coverage hints
# --------------------------------------------------------------------------- #

def _scan_text(text, add):
    if not text:
        return
    for m in _WORD_FOLIO.finditer(text):
        word = m.group(1).lower().rstrip(".")
        add(_WORD_LABEL.get(word, word.capitalize()),
            int(m.group(2)), int(m.group(3)))


def page_hints(con, bid):
    """Page-numbering gaps for a letter ("Seite 2/4, 3/4, 4/4 — 1/4 not found").

    Merged per page total (bilingual Seite/Page mentions of one scheme are
    the same scheme); the display label = most frequent, ties per preference.
    """
    schemes = {}  # total -> {"seen": set(n), "labels": {label: count}}

    def add(label, n, m):
        if 2 <= m <= MAX_TOTAL and 1 <= n <= m:
            d = schemes.setdefault(m, {"seen": set(), "labels": {}})
            d["seen"].add(n)
            d["labels"][label] = d["labels"].get(label, 0) + 1

    for r in con.execute(
            "SELECT f.value FROM facts f JOIN sheets s ON s.id=f.sheet_id "
            "WHERE s.batch_id=?", (bid,)):
        _scan_text(r["value"], add)
    for r in con.execute(
            "SELECT a.raw FROM analyses a JOIN sheets s ON s.id=a.sheet_id "
            "WHERE s.batch_id=? AND a.status='ok' AND a.raw IS NOT NULL", (bid,)):
        _scan_text(r["raw"], add)
    out = []
    for m, d in schemes.items():
        seen = d["seen"]
        missing = sorted(set(range(1, m + 1)) - seen)
        if not missing or (len(seen) < 2 and len(missing) < 2):
            continue
        pref = {lab: i for i, lab in enumerate(_LABEL_PREF)}
        label = max(d["labels"].items(),
                    key=lambda kv: (kv[1], -pref.get(kv[0], len(pref))))[0]
        out.append({
            "label": label, "total": m,
            "seen": sorted(seen), "missing": missing,
            "seen_txt": ", ".join(f"{n}/{m}" for n in sorted(seen)),
            "missing_txt": ", ".join(f"{n}/{m}" for n in missing),
        })
    out.sort(key=lambda h: (0 if h["label"] in ("Seite", "Page") else 1, -h["total"]))
    return out


# --------------------------------------------------------------------------- #
# combined
# --------------------------------------------------------------------------- #

def hints_for(bid, con=None):
    """Both hint kinds for one letter. Never raises — hints are advisory."""
    own = con is None
    con = con if con is not None else db.connect()
    try:
        return {"dups": duplicate_hints(con, bid), "pages": page_hints(con, bid)}
    except Exception:
        return {"dups": [], "pages": []}
    finally:
        if own:
            con.close()
