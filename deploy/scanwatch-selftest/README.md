# scanwatch selftest

Automated regression for the on-demand (button-driven) scan flow — runs WITHOUT
the physical scanner (plan-driven fake pull script + pingable localhost).

    python3 deploy/scanwatch-selftest/session-test.py

Covers (48 checks): request → capture → session open/extend → finish; **no
spontaneous attempts** (ICMP watch only — ambient scanning was removed
2026-10-12 at the user's request); kill -9 orphan recovery on restart; idle
timeout; offline request note; intake registration (closed session = one batch
+ merged PDF, member runs skipped, open session held, stale-open registered,
idempotent re-ingest); board API (scanner status, scan request file, offline
refusal, trash: archive + mark + hide + banner).

Last run 2026-10-12: **ALL PASS** (48 checks, ~50 s).

## fake-scan.py — plan-driven fake scanner (used by the tests)

Env:
  `SWTEST_PLAN`  JSON list of steps: `"empty" | "capture" | "busy" | "error"`
                 (default `/tmp/swtest/plan.json`; past the end → "empty")
  `SWTEST_N`     call-counter file (default `/tmp/swtest/n`, created on first call)

Each call consumes the next step. Captures create `<--out>/pull_<ts>_<n>/` with
`manifest.json` (2 sheets) + dummy JPEGs and print `RUNNER_DIR=<dir>`.
Exit codes mirror brother-pull-scan.py: empty=3, busy=2, error=4, capture=0.
