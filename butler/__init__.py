"""BUTLER — mail triage & task planning (hermes-rpi5).

Package layout:
  credentials.py  central credential resolution (Hermes .env is the source of truth)
  db.py           SQLite registry (batches, sheets, ...)
  intake.py       scan-inbox watcher -> registry (idempotent)
  notify.py       Telegram notifications (via the Hermes bot)
  web.py          Kanban board (browse scanned mails)
  cli.py          command line: init | ingest | status
"""

__version__ = "0.1.0"
ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
