"""Button-push intake: PDFs uploaded by the scanner's physical Scan button
become letter batches.

The ADS-1000W's "Scan to FTP" job (physical button, profile PiScans) uploads
ONE PDF per job to the FTP receiver's root (~/scans/incoming). This module:

  1. finds completed PDFs there — uploads keep the mtime fresh, so a file
     whose mtime has settled for MIN_AGE seconds is a finished one;
  2. renders every page at 300 dpi into <incoming>/<stem>/sheets/ as
     sheet_XX_front.jpg / sheet_XX_back.jpg — consecutive pages pair as
     front/back of one sheet (the device profile scans duplex "long edge";
     an odd tail page becomes a front-only sheet);
  3. moves the original PDF into that folder (files are never deleted);
  4. registers the whole thing as ONE batch: key `push/<stem>`, source
     `push`, sheets start in `inbox` so the normal analysis gates apply.

Idempotent — the batch key guards re-processing; a `push.json` marker inside
the folder keeps the generic run scanner (intake.find_runs) away from it.
The device names files from its "File Name" preset (e.g. Report_005457.pdf).
"""
import datetime
import json
import os
import pathlib
import shutil
import time

from . import db  # noqa: F401  (callers pass a connection; kept for symmetry)
from . import coverage

SCANS_ROOT = pathlib.Path(os.environ.get(
    "BUTLER_SCANS_DIR", str(pathlib.Path.home() / "scans")))
INCOMING = SCANS_ROOT / "incoming"
MIN_AGE = 20          # s — uploads keep mtime fresh; settled = complete
_last_size = {}       # path -> size seen at the previous call (growing check)


def find_ready():
    """Top-level PDFs in incoming/ that look fully uploaded."""
    out = []
    if not INCOMING.exists():
        return out
    now = time.time()
    for p in sorted(INCOMING.glob("*.pdf")):
        try:
            st = p.stat()
        except OSError:
            continue
        prev = _last_size.get(str(p))
        _last_size[str(p)] = st.st_size
        if st.st_size == 0 or now - st.st_mtime < MIN_AGE:
            continue
        if prev is not None and prev != st.st_size:
            continue                      # still growing
        out.append(p)
    return out


def _pair_pages(n):
    """1,2 -> sheet 1; 3,4 -> sheet 2; an odd tail -> front-only sheet."""
    pairs = []
    for i in range(0, n, 2):
        front = i + 1
        back = i + 2 if i + 2 <= n else None
        pairs.append((front, back))
    return pairs


def _render(folder, pairs, pdf):
    """Render every page into folder/sheets/ as JPEGs. Returns page count."""
    import fitz
    sheets_dir = folder / "sheets"
    sheets_dir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(str(pdf))
    try:
        n = doc.page_count
        for idx, (f, b) in enumerate(pairs, start=1):
            for side, pno in (("front", f), ("back", b)):
                if pno is None:
                    continue
                page = doc.load_page(pno - 1)
                pix = page.get_pixmap(dpi=300)
                pix.pil_save(str(sheets_dir / f"sheet_{idx:02d}_{side}.jpg"),
                             quality=90)
        return n
    finally:
        doc.close()


def process_pending(con, verbose=False):
    """Register any ready pushed PDFs. Returns [(key, sheet_count), ...]."""
    done = []
    for pdf in find_ready():
        stem = pdf.stem
        key = f"push/{stem}"
        if con.execute("SELECT 1 FROM batches WHERE key=?", (key,)).fetchone():
            continue
        folder = INCOMING / stem
        try:
            st = pdf.stat()
            ts = datetime.datetime.fromtimestamp(st.st_mtime).strftime(
                "%Y%m%d_%H%M%S")
            import fitz
            with fitz.open(str(pdf)) as d:
                pages = d.page_count
            pairs = _pair_pages(pages)
            _render(folder, pairs, pdf)
            shutil.move(str(pdf), str(folder / pdf.name))
            (folder / "push.json").write_text(json.dumps({
                "source": "scanner-button-ftp",
                "original": pdf.name,
                "pages": pages,
                "sheets": len(pairs),
                "received_ts": st.st_mtime,
            }, indent=1))
            cur = con.execute(
                "INSERT INTO batches(key, ts, source, folder, status, note, "
                "created_at) VALUES (?,?,?,?,?,?,?)",
                (key, ts, "push", str(folder), "registered",
                 f"{len(pairs)} sheets · button push", time.time()),
            )
            bid = cur.lastrowid
            for idx, (f, b) in enumerate(pairs, start=1):
                con.execute(
                    "INSERT INTO sheets(batch_id, sheet_no, front, back, "
                    "meta_json) VALUES (?,?,?,?,?)",
                    (bid, idx, f"sheets/sheet_{idx:02d}_front.jpg",
                     f"sheets/sheet_{idx:02d}_back.jpg" if b else "",
                     json.dumps({"push": True, "pages": [f, b]})),
                )
            con.commit()
            coverage.index_batch(bid, con=con)   # face identity for re-scan detection
            done.append((key, len(pairs)))
            if verbose:
                print(f"[push] registered {key}: {pages} page(s) -> "
                      f"{len(pairs)} sheet(s)")
        except Exception as e:
            con.rollback()
            if verbose:
                print(f"[push] {pdf.name}: failed ({type(e).__name__}: {e}); "
                      f"will retry on a later pass")
    return done
