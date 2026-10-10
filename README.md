# BUTLER — mail triage & task planning

Home (later: business) assistant for physical postal mail. Scans go in;
per-mail analysis, a browsable board, tasks and reminders come out —
everything cited, nothing sent or deleted without approval.

**Full spec: [PLAN.md](PLAN.md)** — single spec file, git-tracked.

## Architecture

```
scanner -> intake watcher -> registry (SQLite) -> analyzer (DeepSeek vision)
                                                    |
 board (browse/review) <-- scheduler & tasks <------+
        |
 Telegram (per-mail notifications)
```

## Services (systemd --user, hermes-rpi5)

| unit               | what                                      | port |
|--------------------|-------------------------------------------|------|
| `butler-intake`    | watches scan dirs, registers batches      | —    |
| `butler-analyze`   | DeepSeek vision analysis -> cited facts   | —    |
| `butler-board`     | Kanban board (browse/review)              | 9500 |

## Quickstart

```bash
python3 -m butler.cli init          # create/migrate registry
python3 -m butler.cli ingest        # register new scan runs
python3 -m butler.cli analyze       # analyze all pending sheets
python3 -m butler.cli analyze --sheet 1 --no-notify   # one sheet, quiet
python3 -m butler.cli analyze-loop  # daemon: continuous analysis
python3 -m butler.web               # board on :9500
python3 -m butler.cli status        # what the registry knows
```

## Layout

- `butler/` — package: `db` (SQLite), `intake` (watcher), `analyze` (vision
  analysis), `llm` (provider adapter — DeepSeek now, local monster later),
  `notify` (Telegram), `web` (Flask board), `cli`, `credentials`
- `templates/`, `static/` — board UI
- `golden/` — labeled test set + checker (QA)
- `var/` — SQLite DB (not in git)

## Secrets

Auto-sourced from the Hermes store (`~/.hermes/.env`, `DEEPSEEK_API_KEY`).
No keys in this repo. See `~/.config/butler/README.txt`.
