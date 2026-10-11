# scan-watch selftest

Two layers, both run WITHOUT the physical scanner (fake pull script + pingable
localhost).

## 1. Automated session/button regression (primary)

    python3 deploy/scanwatch-selftest/session-test.py

Covers (38 checks): request → capture → session open/extend → finish; ambient
attempts paused while a session is open and resumed after; kill -9 orphan
recovery on restart; idle timeout; offline request note; intake registration
(closed session = one batch + merged PDF, member runs skipped, open session
held, stale-open registered, idempotent re-ingest); board API (scanner status,
scan request file, offline refusal, trash: archive + mark + hide + banner).

Last run 2026-10-11: **ALL PASS** (38 checks, 33 s).

## 2. Manual state-machine run (scan-watch main loop)

    rm -rf /tmp/swtest && cp -r deploy/scanwatch-selftest /tmp/swtest
    timeout 58 python3 deploy/scan-watch.py --config /tmp/swtest/cfg.json
    cat /tmp/swtest/log.txt

Expect (cfg durations: scan_interval=8 s, cooldown=3 s, busy_backoff=10 s):
  1. `scanner ONLINE — first scan attempt in 8s` — settle window honoured
  2. first attempt at +8 s → `empty rc=3` → `CAPTURED rc=0` → `BUSY ×3` →
     wedge ALERT + suspend
  3. clean `stopping (signal)` when the timeout sends SIGTERM.

Last run 2026-10-11: PASS (all branches).

## fake-scan.py — plan-driven fake scanner

Env:
  `SWTEST_PLAN`  JSON list of steps: `"empty" | "capture" | "busy" | "error"`
                 (default `/tmp/swtest/plan.json`; past the end → "empty")
  `SWTEST_N`     call-counter file (default `/tmp/swtest/n`, created on first call)

Each call consumes the next step. Captures create `<--out>/pull_<ts>_<n>/` with
`manifest.json` (2 sheets) + dummy JPEGs and print `RUNNER_DIR=<dir>`.
Exit codes mirror brother-pull-scan.py: empty=3, busy=2, error=4, capture=0.
