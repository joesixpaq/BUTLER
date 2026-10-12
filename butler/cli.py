"""BUTLER CLI: python3 -m butler.cli {init|ingest|ingest-loop|analyze|analyze-loop|llm|status|extract|tasks|task|remind|remind-loop}"""
import argparse
import json
import sys
import time

from . import db, intake, llm, qrpay, tasks
from . import analyze as analyzer


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


def cmd_analyze(a):
    db.init()
    if a.sheet:
        r = analyzer.analyze_sheet(a.sheet, notify=not a.no_notify, verbose=True)
        print(json.dumps(r, ensure_ascii=False, indent=2)[:3000])
        return 0
    done = analyzer.run_pending(notify=not a.no_notify, verbose=True, force=a.force)
    print("analyzed:", done)
    return 0


def cmd_analyze_loop(a):
    db.init()
    print(f"analysis loop every {a.interval}s", flush=True)
    while True:
        try:
            done = analyzer.run_pending(notify=True, verbose=True)
            if done:
                print("analyzed:", done, flush=True)
        except Exception as e:
            print(f"analyze error: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        time.sleep(a.interval)


def cmd_extract(a):
    """Backfill the P3 enrichment for stored analyses (QR decode + deadline tasks)."""
    db.init()
    con = db.connect()
    try:
        if a.sheet:
            ids = [a.sheet]
        else:
            ids = [r["id"] for r in con.execute(
                "SELECT s.id FROM sheets s JOIN batches b ON b.id=s.batch_id "
                "WHERE COALESCE(b.status,'')!='trashed' AND EXISTS("
                "SELECT 1 FROM analyses a WHERE a.sheet_id=s.id AND a.status='ok') "
                "ORDER BY s.id")]
    finally:
        con.close()
    n_pay, n_task = 0, 0
    for sid in ids:
        p = qrpay.decode_for_sheet(sid, verbose=True)
        new = tasks.extract_for_sheet(sid)
        n_pay += 1 if p else 0
        n_task += len(new)
    print(f"extract: {len(ids)} sheet(s) · {n_pay} payment(s) · {n_task} new task(s)")
    return 0


def cmd_tasks(a):
    db.init()
    con = db.connect()
    try:
        rows = con.execute(
            "SELECT t.* FROM tasks t JOIN sheets s ON s.id=t.sheet_id "
            "JOIN batches b ON b.id=s.batch_id "
            "WHERE COALESCE(b.status,'')!='trashed' "
            "ORDER BY (t.due IS NULL), t.due, t.id").fetchall()
        sent = {}
        for r in con.execute("SELECT task_id, offset_days FROM reminders "
                             "WHERE sent_at IS NOT NULL"):
            sent.setdefault(r["task_id"], set()).add(r["offset_days"])
    finally:
        con.close()
    if not rows:
        print("no tasks")
        return 0
    from datetime import date as _d
    today = _d.today()
    for t in rows:
        extra = ""
        if t["status"] == "open" and t["due"]:
            d = (_d.fromisoformat(t["due"]) - today).days
            s = ",".join(str(o) for o in sorted(
                (o for o in sent.get(t["id"], set()) if o >= 0), reverse=True)) or "–"
            pend = [o for o in tasks.REMIND_OFFSETS if d <= o and o not in sent.get(t["id"], set())]
            extra = f" · sent {s}"
            if d < 0:
                extra += " · OVERDUE" if tasks.OVERDUE_OFFSET in sent.get(t["id"], set()) else " · overdue (ping pending)"
            elif pend:
                extra += f" · next {min(pend)}"
        print(f"task {t['id']:>3}  {t['due'] or '—' :<10}  {t['status']:<9} "
              f"sheet {t['sheet_id']:<3} [{t['source']}] {t['title']}{extra}")
    return 0


def cmd_task_add(a):
    db.init()
    tid = tasks.add_task(a.sheet, a.title, due=a.due)
    print(f"task {tid} added (sheet {a.sheet}, due {a.due or '—'})")
    return 0


def cmd_task_set(a):
    db.init()
    tasks.set_task_status(a.id, a.status)
    print(f"task {a.id} -> {a.status}")
    return 0


def cmd_remind(a):
    from datetime import date as _d
    asof = _d.fromisoformat(a.asof) if a.asof else None
    out = tasks.run_reminders(dry=a.dry, asof=asof, verbose=True)
    print(f"reminders: {len(out)} {'would send' if a.dry else 'sent'}")
    for m in out:
        print("---")
        print(m)
    return 0


def cmd_remind_loop(a):
    db.init()
    print(f"reminder loop every {a.interval}s "
          f"(offsets {tasks.REMIND_OFFSETS}, not before {tasks.REMIND_HOUR}:00)", flush=True)
    while True:
        try:
            for m in tasks.run_reminders(verbose=True):
                print("[remind] fired:\n" + m, flush=True)
        except Exception as e:  # keep the daemon alive no matter what
            print(f"remind error: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        time.sleep(a.interval)


def cmd_llm(a):
    if a.set:
        path = llm.set_provider(a.set)
        cfg = llm.get_config()
        print(f"analysis backend -> {cfg['provider']}  ({path})")
        print(f"  base_url: {cfg['base_url']}")
        print(f"  model:    {cfg['model'] or 'auto (resolved from /v1/models)'}")
        print("  (read on every analysis — no service restart needed)")
        return 0
    cfg = llm.get_config()
    print(f"analysis backend: {cfg['provider']}")
    print(f"  base_url: {cfg['base_url']}")
    print(f"  model:    {cfg['model'] or 'auto (resolved from /v1/models)'}")
    print(f"  timeout:  {cfg['timeout_s']}s · max_tokens {cfg['max_tokens']}")
    if llm.CONFIG_PATH.exists():
        print(f"  config:   {llm.CONFIG_PATH}")
    else:
        print(f"  config:   {llm.CONFIG_PATH} (not set — defaults)")
    print()
    for name in sorted(llm.PROVIDERS):
        ok, detail = llm.probe(name)
        print(f"  probe {name:9s} {'OK ' if ok else 'FAIL'} {detail}")
    return 0


def cmd_status(a):
    db.init()
    con = db.connect()
    try:
        for b in con.execute("SELECT * FROM batches ORDER BY id"):
            n = _col(con, "sheets", "batch_id=?", b["id"])
            print(f"batch #{b['id']}  {b['ts']}  {b['key']}  sheets={n}  {b['note'] or ''}")
        print()
        for s in con.execute(
                "SELECT s.id, s.sheet_no, s.status, a.status AS ast, a.error AS aerr "
                "FROM sheets s LEFT JOIN analyses a ON a.sheet_id=s.id ORDER BY s.id"):
            line = (f"sheet {s['id']} (no {s['sheet_no']}): {s['status']} · "
                    f"analysis={s['ast'] or 'pending'}")
            if s["aerr"]:
                line += f" · {str(s['aerr'])[:80]}"
            print(line)
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

    p = sub.add_parser("analyze", help="analyze pending sheets (or one with --sheet N)")
    p.add_argument("--sheet", type=int, default=None)
    p.add_argument("--no-notify", action="store_true")
    p.add_argument("--force", action="store_true",
                   help="ignore the hold + schedule gates (one-off runs)")
    p.set_defaults(fn=cmd_analyze)

    p = sub.add_parser("analyze-loop", help="analyze pending sheets continuously (daemon)")
    p.add_argument("--interval", type=int, default=20)
    p.set_defaults(fn=cmd_analyze_loop)

    p = sub.add_parser("llm", help="show or switch the analysis LLM backend")
    p.add_argument("set", nargs="?", choices=sorted(llm.PROVIDERS), metavar="PROVIDER",
                   help="switch backend: deepseek | monster (writes ~/.config/butler/llm.json)")
    p.set_defaults(fn=cmd_llm)

    sub.add_parser("status", help="list batches and sheets").set_defaults(fn=cmd_status)

    p = sub.add_parser("extract", help="derive payments (Swiss QR) + deadline tasks from stored analyses")
    p.add_argument("--sheet", type=int, default=None, help="one sheet id (default: all analyzed)")
    p.set_defaults(fn=cmd_extract)

    sub.add_parser("tasks", help="list tasks with reminder progress").set_defaults(fn=cmd_tasks)

    p = sub.add_parser("task", help="add or update one task")
    tsp = p.add_subparsers(dest="action", required=True)
    pa = tsp.add_parser("add")
    pa.add_argument("--sheet", type=int, required=True)
    pa.add_argument("--title", required=True)
    pa.add_argument("--due", default=None, help="YYYY-MM-DD or DD.MM.YYYY (optional)")
    pa.set_defaults(fn=cmd_task_add)
    ps = tsp.add_parser("set")
    ps.add_argument("--id", type=int, required=True)
    ps.add_argument("--status", choices=tasks.TASK_STATUSES, required=True)
    ps.set_defaults(fn=cmd_task_set)

    p = sub.add_parser("remind", help="run one reminder pass (Telegram, idempotent)")
    p.add_argument("--dry", action="store_true", help="show what would be sent — send/mark nothing")
    p.add_argument("--asof", default=None, metavar="YYYY-MM-DD", help="pretend today (QA)")
    p.set_defaults(fn=cmd_remind)

    p = sub.add_parser("remind-loop", help="reminder daemon (systemd: butler-remind.service)")
    p.add_argument("--interval", type=int, default=600)
    p.set_defaults(fn=cmd_remind_loop)

    a = ap.parse_args()
    sys.exit(a.fn(a))


if __name__ == "__main__":
    main()
