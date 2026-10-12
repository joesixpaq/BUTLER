# BUTLER — mail triage & task planning

Home (later: business) assistant for physical postal mail. Scans go in;
per-mail analysis, a browsable board, tasks and reminders come out —
everything cited, nothing sent or deleted without approval.

**Full spec: [PLAN.md](PLAN.md)** — single spec file, git-tracked.

## Architecture

```
scanner -> intake watcher -> registry (SQLite) -> analyzer (vision LLM -> cited facts)
                                                    |
 board (browse/review) <-- scheduler & tasks <------+
        |
 Telegram (per-mail notifications)
```

## Services (systemd --user, hermes-rpi5)

| unit               | what                                      | port |
|--------------------|-------------------------------------------|------|
| `butler-intake`    | watches scan dirs, registers batches      | —    |
| `butler-analyze`   | vision analysis -> cited facts            | —    |
| `butler-board`     | Kanban board (browse/review)              | 9500 |
| `scan-watch`       | ADS-1000W: ICMP watch + on-demand scan sessions | —  |
| `butler-remind`    | deadline reminders -> Telegram (+ overdue) | —   |

Scans happen only on request — no ambient polling. The board's scanner panel
drives one-by-one feeding: *Scan sheet* / *Finish batch* write a request file
that `scan-watch` executes (single device controller); a closed session
registers as one batch. Each batch can be archived ("trash", never deleted)
from its letter or scan view — the browser button returns to the board; API
posts (no form content type) get JSON with the archived paths.

## Board (letters)

A batch = the sheets of **one letter**. The board shows one card per letter,
with the statuses stacked **vertically** (Inbox … Filed/Noise as rows) and the
cards flowing **horizontally** inside each row (~6 per row, never h-scrolls).
**Click** a card to open the letter in a new tab; **drag** it to move the whole
letter between statuses. The letter page lists
its scans (thumbnails → scan view) and offers **per-scan removal** — a whole
scan or just one side (e.g. a blank back); removals are restorable and files
are never deleted. The letter page also carries the payment prep, aggregated
tasks & reminders, and the letter-wide status/trash. The scan view (zoom)
shows one sheet's images and cited facts; its status control moves the whole
letter.

## Quickstart

```bash
python3 -m butler.cli init          # create/migrate registry
python3 -m butler.cli ingest        # register new scan runs
python3 -m butler.cli analyze       # analyze all pending sheets
python3 -m butler.cli analyze --sheet 1 --no-notify   # one sheet, quiet
python3 -m butler.cli analyze-loop  # daemon: continuous analysis
python3 -m butler.cli extract       # P3 backfill: QR payment decode + deadline tasks
python3 -m butler.cli tasks         # list tasks + reminder progress
python3 -m butler.cli task add --sheet 5 --title "pay invoice" --due 2026-11-04
python3 -m butler.cli remind        # one reminder pass (--dry = show only, --asof YYYY-MM-DD = QA)
python3 -m butler.cli llm           # show analysis backend + probe endpoints
python3 -m butler.cli llm monster   # switch analysis to the local monster model
python3 -m butler.web               # board on :9500
python3 -m butler.cli status        # what the registry knows
```

## Layout

- `butler/` — package: `db` (SQLite), `intake` (watcher), `analyze` (vision
  analysis), `llm` (provider adapter — DeepSeek default, local `monster` option),
  `tasks` (deadlines, reminder engine, `.ics`), `qrpay` (Swiss QR payment prep),
  `notify` (Telegram), `web` (Flask board), `cli`, `credentials`
- `templates/`, `static/` — board UI
- `golden/` — labeled test set + checker (QA)
- `var/` — SQLite DB (not in git)

## Secrets

Auto-sourced from the Hermes store (`~/.hermes/.env`, `DEEPSEEK_API_KEY`).
No keys in this repo. See `~/.config/butler/README.txt`.
