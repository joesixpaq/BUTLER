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

Scans happen only on request — no ambient polling. The board's scanner panel
drives one-by-one feeding: *Scan sheet* / *Finish batch* write a request file
that `scan-watch` executes (single device controller); a closed session
registers as one batch. Each batch can be archived ("trash", never deleted)
from its sheet view.

## Quickstart

```bash
python3 -m butler.cli init          # create/migrate registry
python3 -m butler.cli ingest        # register new scan runs
python3 -m butler.cli analyze       # analyze all pending sheets
python3 -m butler.cli analyze --sheet 1 --no-notify   # one sheet, quiet
python3 -m butler.cli analyze-loop  # daemon: continuous analysis
python3 -m butler.cli llm           # show analysis backend + probe endpoints
python3 -m butler.cli llm monster   # switch analysis to the local monster model
python3 -m butler.web               # board on :9500
python3 -m butler.cli status        # what the registry knows
```

## Layout

- `butler/` — package: `db` (SQLite), `intake` (watcher), `analyze` (vision
  analysis), `llm` (provider adapter — DeepSeek default, local `monster` option),
  `notify` (Telegram), `web` (Flask board), `cli`, `credentials`
- `templates/`, `static/` — board UI
- `golden/` — labeled test set + checker (QA)
- `var/` — SQLite DB (not in git)

## Secrets

Auto-sourced from the Hermes store (`~/.hermes/.env`, `DEEPSEEK_API_KEY`).
No keys in this repo. See `~/.config/butler/README.txt`.
