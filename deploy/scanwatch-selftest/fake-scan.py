#!/usr/bin/env python3
"""Fake pull-scan for scan-watch self-tests — plan-driven (no hardware).

Env:
  SWTEST_PLAN  path to a JSON list of steps: "empty" | "capture" | "busy" | "error"
               (default /tmp/swtest/plan.json)
  SWTEST_N     path to the call-counter file, created on first call
               (default /tmp/swtest/n)

Each call consumes the next step (past the end: "empty"). Captures create
<--out>/pull_<ts>_<n>/ with manifest.json + 2 dummy sheets and print
RUNNER_DIR=<dir>. rc: empty=3, busy=2, error=4, capture=0 (mirrors
brother-pull-scan.py's contract).
"""
import json
import os
import sys
import time


def _arg(name, default=None):
    a = sys.argv
    return a[a.index(name) + 1] if name in a else default


plan_path = os.environ.get("SWTEST_PLAN", "/tmp/swtest/plan.json")
n_path = os.environ.get("SWTEST_N", "/tmp/swtest/n")
out = _arg("--out", "/tmp/swtest/out") or "/tmp/swtest/out"

plan = json.load(open(plan_path))
n = int(open(n_path).read().strip() or "0") if os.path.exists(n_path) else 0
step = plan[n] if n < len(plan) else "empty"
open(n_path, "w").write(str(n + 1))

if step == "empty":
    print("FEEDER EMPTY (test)", file=sys.stderr)
    sys.exit(3)
if step == "busy":
    print("SCANNER BUSY (-NG 401) (test)", file=sys.stderr)
    sys.exit(2)
if step == "error":
    print("TEST ERROR (test)", file=sys.stderr)
    sys.exit(4)

run = os.path.join(out, time.strftime("pull_%Y%m%d_%H%M%S") + f"_{n}")
os.makedirs(os.path.join(run, "sheets"), exist_ok=True)
sheets = []
for i in (1, 2):
    names = {}
    for side in ("front", "back"):
        fn = f"sheet_{i:02d}_{side}.jpg"
        with open(os.path.join(run, "sheets", fn), "wb") as f:
            f.write(b"\xff\xd8\xff\xd9")  # minimal jpeg bytes
        names[side] = f"sheets/{fn}"
    sheets.append({"index": i, **names})
with open(os.path.join(run, "manifest.json"), "w") as f:
    json.dump({"scan_timestamp": time.strftime("%Y%m%d_%H%M%S"),
               "sheets": sheets, "stream_end": "done-marker"}, f)
print(f"RUNNER_DIR={run}")
sys.exit(0)
