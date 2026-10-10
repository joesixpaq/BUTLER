#!/usr/bin/env python3
"""Fake pull-scan for scan-watch self-test: scripted rc sequence 3,0,2,2,2..."""
import os
import sys

nfile = "/tmp/swtest/n"
n = int(open(nfile).read().strip() or "0") if os.path.exists(nfile) else 0
n += 1
open(nfile, "w").write(str(n))

if n == 1:
    print("FEEDER EMPTY (test)", file=sys.stderr)
    sys.exit(3)
if n == 2:
    print("RUNNER_DIR=/tmp/swtest/run_2")
    print("SIDES=2 SHEETS=1 INCOMPLETE=0")
    sys.exit(0)
print("SCANNER BUSY (-NG 401) (test)", file=sys.stderr)
sys.exit(2)
