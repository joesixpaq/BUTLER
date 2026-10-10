# scan-watch selftest

Scripted regression for `../scan-watch.py` — exercises the full state machine
WITHOUT the physical scanner (fake pull script + pingable localhost).

    cp -r deploy/scanwatch-selftest /tmp/swtest
    timeout 58 python3 ~/lan-plan/scan-watch.py --config /tmp/swtest/cfg.json
    cat /tmp/swtest/log.txt

Expected (cfg durations: scan_interval=8 s, cooldown=3 s, busy_backoff=10 s):
  1. `scanner ONLINE — first scan attempt in 8s`, first attempt at +8 s  → settle window
  2. empty rc=3 → CAPTURED rc=0 (RUNNER_DIR parsed) → BUSY ×3 → wedge ALERT + suspend
  3. clean `stopping (signal)` when the timeout sends SIGTERM.

Fake rc sequence per call (see fake-scan.py): 3, 0, 2, 2, 2...
First run 2026-10-11: PASS (all branches verified).
