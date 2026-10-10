"""BUTLER CLI:  python3 -m butler.cli {init|ingest|ingest-loop|status}"""
import argparse
import sys
import time

from . import db, intake


def _col(con, table, where, *args):
    return con.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", args).fetchone()[0]


def cmd_init(a):
    db.init()
    print("db:", db.DB_PATH)
    return 0


def cmd_ingest(a):
    new, legacy = intake.ingest(notify=not a.no_notify, verbose=True)
    print(f"new batches: {new}")
    print(f"legacy skipped: {legacy}")
    return 0


def cmd_ingest_loop(a):
    db.init()
    print(f"watching {[str(d) for d in intake.SCAN_DIRS]} every {a.interval}s", flush=True)
    while True:
        try:
            intake.ingest(notify=True)
        except Exception as e:  # keep the watcher alive no matter what
            print(f"ingest error: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        time.sleep(a.interval)


def cmd_status(a):
    db.init()
    con = db.connect()
    try:
        for b in con.execute("SELECT * FROM batches ORDER BY id"):
            n = _col(con, "sheets", "batch_id=?", b["id"])
            print(f"#{b['id']}  {b['ts']}  {b['key']}  sheets={n}  {b['note'] or ''}")
    finally:
        con.close()
    return 0


def main():
    ap = argparse.ArgumentParser(prog="butler", description="BUTLER registry CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create/migrate the registry database").set_defaults(fn=cmd_init)

    p = sub.add_parser("ingest", help="register new scan runs")
    p.add_argument("--no-notify", action="store_true")
    p.set_defaults(fn=cmd_ingest)

    p = sub.add_parser("ingest-loop", help="watch scan dirs and register (daemon)")
    p.add_argument("--interval", type=int, default=30)
    p.set_defaults(fn=cmd_ingest_loop)

    sub.add_parser("status", help="list registered batches").set_defaults(fn=cmd_status)

    a = ap.parse_args()
    sys.exit(a.fn(a))


if __name__ == "__main__":
    main()
