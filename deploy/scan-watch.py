#!/usr/bin/env python3
"""scan-watch — automatic ADS-1000W scan grabber for the BUTLER mail pipeline.

Behaviour
  OFFLINE: ICMP ping every `ping_interval` (default 90 s; ICMP only — never a
           TCP probe of 54921, which can wedge the device).
  On first detection online: wait one `scan_interval` (default 120 s) before the
  first attempt (settle window), then attempt every `scan_interval` while online.
  A captured run lands in `scan_out` (~/scans/direct/pull_<ts>/) where the
  butler-intake service registers it automatically — no notification here
  (BUTLER pings per mail, not per batch).

Exit-code handling (brother-pull-scan.py)
  0 capture      -> log, 60 s cooldown, continue
  3 empty (0xC2) -> log, continue (cadence relaxes to `idle_interval` after
                    `idle_stretch_after` consecutive empties)
  2 busy         -> `busy_backoff` (10 min); `busy_alert_limit` (3) in a row ->
                    Telegram alert + suspend attempts until the device power-cycles
  4/5 errors     -> retry next tick; 3 in a row -> Telegram alert.
                    rc=5 (partial capture) alerts immediately — possible jam.
  anything else  -> logged as error; ping check decides offline transition.

Safety: single-flight flock (one instance ever); attempts strictly sequential.
Config:  ~/.config/butler/scanwatch.json  (written with defaults on first run)
Log:     ~/lan-plan/scan-watch.log   State: ~/lan-plan/scan-watch.state.json
"""
import argparse
import datetime
import fcntl
import json
import os
import signal
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
DEFAULTS = {
    "host": "192.168.0.104",
    "pull_script": os.path.join(HOME, "lan-plan/brother-pull-scan.py"),
    "scan_out": os.path.join(HOME, "scans/direct"),
    "duplex": True,
    "mode": "color",
    "ping_interval": 90,        # s, OFFLINE mode (ICMP only)
    "scan_interval": 120,       # s, ONLINE mode
    "idle_stretch_after": 10,   # consecutive empties before relaxing cadence
    "idle_interval": 300,       # s, relaxed cadence once idle
    "busy_backoff": 600,        # s after a busy reply
    "busy_alert_limit": 3,      # consecutive busy -> alert + suspend
    "error_alert_limit": 3,     # consecutive errors -> alert
    "capture_cooldown": 60,     # s after a capture
    "attempt_timeout": 1000,    # s hard cap per attempt (script's own: 900)
    "log_file": os.path.join(HOME, "lan-plan/scan-watch.log"),
    "state_file": os.path.join(HOME, "lan-plan/scan-watch.state.json"),
    "telegram_alerts": True,
}
STOP = False


def now_iso():
    return datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def load_cfg(path):
    cfg = dict(DEFAULTS)
    if os.path.exists(path):
        try:
            cfg.update(json.load(open(path)))
        except Exception as e:
            print(f"config parse error ({e}); using defaults", file=sys.stderr)
    else:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                json.dump(cfg, f, indent=2)
        except Exception:
            pass
    return cfg


def log(cfg, msg, level="INFO"):
    line = f"{now_iso()} {level} {msg}"
    print(line, flush=True)
    try:
        with open(cfg["log_file"], "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def write_state(cfg, **fields):
    try:
        p = cfg["state_file"]
        s = {}
        if os.path.exists(p):
            try:
                s = json.load(open(p))
            except Exception:
                s = {}
        s.update(fields)
        s["updated"] = now_iso()
        with open(p, "w") as f:
            json.dump(s, f, indent=2)
    except Exception:
        pass


def ping_ok(host):
    try:
        r = subprocess.run(["ping", "-c", "1", "-W", "2", host],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def send_alert(cfg, text):
    ok = False
    if cfg.get("telegram_alerts"):
        try:
            sys.path.insert(0, os.path.join(HOME, "BUTLER"))
            from butler.notify import send_telegram
            ok = bool(send_telegram(text))
        except Exception:
            ok = False
    log(cfg, ("ALERT sent: " if ok else "ALERT (telegram unavailable): ") + text,
        level="ALERT")
    return ok


def sleep_i(sec):
    end = time.time() + sec
    while not STOP and time.time() < end:
        time.sleep(min(1.0, max(0.0, end - time.time())))


def on_signal(signum, frame):
    global STOP
    STOP = True


def attempt(cfg):
    cmd = [sys.executable, cfg["pull_script"], "--out", cfg["scan_out"]]
    if cfg.get("duplex", True):
        cmd.append("--duplex")
    cmd += ["--mode", cfg["mode"]]
    t0 = time.time()
    rc, out, err = None, "", ""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=cfg["attempt_timeout"])
        rc, out, err = p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired as e:
        rc = -1
        out = (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = (e.stderr or b"").decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        err += f"\n[scan-watch] attempt exceeded {cfg['attempt_timeout']}s — child killed"
    dur = time.time() - t0
    runner = None
    for line in out.splitlines():
        if line.startswith("RUNNER_DIR="):
            runner = line.split("=", 1)[1].strip()
    tail = " | ".join(err.strip().splitlines()[-3:]) if err.strip() else ""
    return rc, dur, runner, tail


def main():
    ap = argparse.ArgumentParser(description="ADS-1000W auto-scan watcher (BUTLER)")
    ap.add_argument("--config", default=os.path.join(HOME, ".config/butler/scanwatch.json"))
    ap.add_argument("--once", action="store_true", help="single cycle then exit (testing)")
    args = ap.parse_args()
    cfg = load_cfg(args.config)

    # single-flight: one scan-watch instance ever
    lock_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".scan-watch.lock")
    lock_fh = open(lock_path, "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("scan-watch: another instance is already running — exiting", file=sys.stderr)
        sys.exit(1)

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    if args.once:
        ok = ping_ok(cfg["host"])
        log(cfg, f"--once: scanner reachable={ok}")
        rc = None
        if ok:
            rc, dur, runner, tail = attempt(cfg)
            log(cfg, f"--once: rc={rc} dur={dur:.1f}s runner={runner} {tail}")
        write_state(cfg, mode="once", last_rc=rc)
        return

    log(cfg, f"scan-watch starting (pid {os.getpid()}; host={cfg['host']}; "
             f"ping={cfg['ping_interval']}s; scan={cfg['scan_interval']}s; out={cfg['scan_out']})")
    write_state(cfg, mode="watching")
    online = False
    suspended = False
    busy_streak = err_streak = empty_streak = 0
    partial_alerted = False
    captures = 0

    while not STOP:
        if not online:
            if suspended:
                if not ping_ok(cfg["host"]):
                    suspended = False
                    busy_streak = 0
                    log(cfg, "device offline while suspended — resume normal watch")
                    write_state(cfg, mode="offline")
                else:
                    sleep_i(cfg["ping_interval"])
                    continue
            if not ping_ok(cfg["host"]):
                sleep_i(cfg["ping_interval"])
                continue
            online = True
            suspended = False
            write_state(cfg, mode="online")
            log(cfg, f"scanner ONLINE — first scan attempt in {cfg['scan_interval']}s")
            sleep_i(cfg["scan_interval"])
            if STOP:
                break
            if not ping_ok(cfg["host"]):
                online = False
                log(cfg, "scanner went offline during the settle wait — back to watch")
                write_state(cfg, mode="offline")
                sleep_i(cfg["ping_interval"])
                continue

        rc, dur, runner, tail = attempt(cfg)
        if rc == 0:
            captures += 1
            empty_streak = busy_streak = err_streak = 0
            partial_alerted = False
            log(cfg, f"CAPTURED rc=0 dur={dur:.1f}s run={runner} (session captures: {captures})")
            write_state(cfg, mode="online", last_rc=rc, last_dur=round(dur, 1),
                        last_run=runner, captures=captures)
            sleep_i(cfg["capture_cooldown"])
        elif rc == 3:
            empty_streak += 1
            busy_streak = err_streak = 0
            iv = cfg["idle_interval"] if empty_streak >= cfg["idle_stretch_after"] else cfg["scan_interval"]
            log(cfg, f"empty rc=3 dur={dur:.1f}s (empty streak {empty_streak}; next in {iv}s)")
            write_state(cfg, mode="online", last_rc=rc, last_dur=round(dur, 1),
                        empty_streak=empty_streak)
            sleep_i(iv)
        elif rc == 2:
            busy_streak += 1
            err_streak = 0
            log(cfg, f"BUSY rc=2 dur={dur:.1f}s (busy streak {busy_streak}) — backing off {cfg['busy_backoff']}s")
            if busy_streak >= cfg["busy_alert_limit"]:
                send_alert(cfg, f"BUTLER scan-watch: scanner wedged (busy ×{busy_streak}). "
                                f"If scans should work, power-cycle the ADS-1000W — I'll resume "
                                f"automatically when it comes back.")
                online = False
                suspended = True
                write_state(cfg, mode="suspended", busy_streak=busy_streak)
                sleep_i(60)
            else:
                write_state(cfg, mode="online", last_rc=rc, busy_streak=busy_streak)
                sleep_i(cfg["busy_backoff"])
        else:
            err_streak += 1
            log(cfg, f"ERROR rc={rc} dur={dur:.1f}s (error streak {err_streak}) {tail}")
            if rc == 5 and not partial_alerted:
                send_alert(cfg, f"BUTLER scan-watch: partial capture (rc=5) — check the "
                                f"scanner for a jam. run={runner}")
                partial_alerted = True
            if err_streak >= cfg["error_alert_limit"]:
                send_alert(cfg, f"BUTLER scan-watch: {err_streak} consecutive scan errors "
                                f"(last rc={rc}). Scanner needs a look.")
                err_streak = 0
            write_state(cfg, mode="online", last_rc=rc, err_streak=err_streak)
            if not ping_ok(cfg["host"]):
                online = False
                empty_streak = busy_streak = 0
                write_state(cfg, mode="offline")
                log(cfg, "scanner went offline — pausing attempts")
                sleep_i(cfg["ping_interval"])
            else:
                sleep_i(cfg["scan_interval"])

    log(cfg, "scan-watch stopping (signal)")
    write_state(cfg, mode="stopped")


if __name__ == "__main__":
    main()
