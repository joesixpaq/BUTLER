#!/usr/bin/env python3
"""scan-watch — automatic ADS-1000W scan grabber for the BUTLER mail pipeline.

Behaviour
  OFFLINE: ICMP ping every `ping_interval` (default 90 s; ICMP only — never a
           TCP probe of 54921, which can wedge the device).
  On first detection online: wait one `scan_interval` (default 120 s) before the
  first attempt (settle window), then attempt every `scan_interval` while online
  (relaxing to `idle_interval` after `idle_stretch_after` consecutive empties).
  A captured run lands in `scan_out` (~/scans/direct/pull_<ts>/) where the
  butler-intake service registers it automatically — no notification here
  (BUTLER pings per mail, not per batch).

  MANUAL SESSION MODE (2026-10-11, button-driven one-by-one feeding):
  Anything that writes `request_file` (the board's "Scan sheet" / "Finish batch"
  buttons) drives an immediate, user-paced flow:
    - {"action": "scan"}  -> immediate scan attempt (settle is skipped — the
      user vouches for the feeder). The first capture OPENS a session; further
      captures extend it. Each capture is still one immutable pull_<ts> run;
      membership is recorded in `sessions_dir`/<id>.json, and intake registers
      the session as ONE batch once it is closed.
    - {"action": "finish"} -> closes the session.
    - While a session is open, AMBIENT attempts pause (the user drives); they
      resume after close. The session also closes on: idle timeout
      (`session_idle_close`), scanner going offline, scan-watch stopping, or a
      session found still open at startup (crash recovery).
    - Results (rc, sheet count, note) are written to the state file as
      `last_request` / `session` / `last_session` for the board to display.

Exit-code handling (brother-pull-scan.py)
  0 capture      -> log, `capture_cooldown`, continue
  3 empty (0xC2) -> log, continue (cadence relaxes to `idle_interval` after
                    `idle_stretch_after` consecutive empties)
  2 busy         -> `busy_backoff` (10 min); `busy_alert_limit` (3) in a row ->
                    Telegram alert + suspend attempts until the device power-cycles
  4/5 errors     -> retry next tick; 3 in a row -> Telegram alert.
                    rc=5 (partial capture) alerts immediately — possible jam.
  anything else  -> logged as error; ping check decides offline transition.

Safety: single-flight flock (one instance ever); attempts strictly sequential.
        Manual scan requests are executed by THIS process only — never run a
        pull-scan by hand while scan-watch might be mid-attempt (busy races).
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
    "scan_interval": 120,       # s, ONLINE mode (also the settle window)
    "idle_stretch_after": 10,   # consecutive empties before relaxing cadence
    "idle_interval": 300,       # s, relaxed cadence once idle
    "busy_backoff": 600,        # s after a busy reply
    "busy_alert_limit": 3,      # consecutive busy -> alert + suspend
    "error_alert_limit": 3,     # consecutive errors -> alert
    "capture_cooldown": 60,     # s after a capture
    "attempt_timeout": 1000,    # s hard cap per attempt (script's own: 900)
    "request_file": os.path.join(HOME, "lan-plan/scan-request.json"),
    "sessions_dir": os.path.join(HOME, "scans/sessions"),
    "request_check_interval": 2,   # s, request-file poll cadence while waiting
    "session_idle_close": 900,     # s, auto-close an idle open session
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


def read_state(cfg):
    try:
        with open(cfg["state_file"]) as f:
            return json.load(f)
    except Exception:
        return {}


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
        tmp = p + ".tmp"
        with open(tmp, "w") as f:
            json.dump(s, f, indent=2)
        os.replace(tmp, p)
    except Exception:
        pass


def write_json_atomic(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


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


def read_request(cfg):
    """Consume a pending UI request (board button), if any. Returns dict or None."""
    p = cfg["request_file"]
    try:
        if not os.path.exists(p):
            return None
        with open(p) as f:
            req = json.load(f)
        os.unlink(p)
        return req if isinstance(req, dict) else None
    except Exception:
        try:
            os.unlink(p)
        except Exception:
            pass
        return None


def wait_check(cfg, seconds):
    """Sleep up to `seconds`, checking for requests every request_check_interval.
    Returns the consumed request dict, or None on timeout/STOP."""
    end = time.time() + seconds
    while not STOP and time.time() < end:
        req = read_request(cfg)
        if req:
            return req
        time.sleep(min(cfg["request_check_interval"], max(0.1, end - time.time())))
    return None


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


def run_sheet_count(runner):
    """Number of sheets in a captured run (from its manifest)."""
    try:
        with open(os.path.join(runner, "manifest.json")) as f:
            return len(json.load(f).get("sheets", []))
    except Exception:
        return 0


# --------------------------------------------------------------------------- #
# manual sessions
# --------------------------------------------------------------------------- #

def new_session():
    sid = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return {
        "session_id": sid,
        "started": now_iso(),
        "started_ts": time.time(),
        "closed": None,
        "source": "manual",
        "runs": [],
        "sheets": 0,
        "last_activity": time.time(),
    }


def _session_public(sess):
    return {k: sess.get(k) for k in
            ("session_id", "started", "started_ts", "closed", "closed_reason",
             "source", "runs", "sheets") if k in sess}


def write_session(cfg, sess):
    write_json_atomic(os.path.join(cfg["sessions_dir"], f"{sess['session_id']}.json"),
                      _session_public(sess))


def close_session(cfg, sess, reason):
    sc = dict(sess)
    sc["closed"] = now_iso()
    sc["closed_reason"] = reason
    write_session(cfg, sc)
    log(cfg, f"SESSION CLOSED ({reason}) — {sc['sheets']} sheet(s) in "
             f"{len(sc['runs'])} run(s) -> {sc['session_id']}")
    return sc


def session_state(sess):
    """Compact view for the state file / board UI (None when no session)."""
    if sess is None:
        return None
    return {"open": True, "session_id": sess["session_id"], "started": sess["started"],
            "started_ts": sess["started_ts"], "sheets": sess["sheets"],
            "runs": list(sess["runs"])}


def last_session_state(sc):
    """Compact view of a just-closed session for the state file / board UI."""
    if sc is None:
        return None
    return {"session_id": sc.get("session_id"), "sheets": sc.get("sheets"),
            "runs": len(sc.get("runs") or []), "closed": sc.get("closed"),
            "closed_reason": sc.get("closed_reason")}


def handle_request(cfg, req, session, online, suspended):
    """Execute a board request. Returns (session, result_dict, closed_session)."""
    closed = None
    act = (req or {}).get("action")
    res = {"id": (req or {}).get("id"), "action": act, "ts": time.time()}
    if act == "scan":
        if suspended:
            res.update(rc=None, note="scanner suspended (power-cycle needed)")
        elif not online:
            res.update(rc=None, note="scanner offline")
        else:
            rc, dur, runner, tail = attempt(cfg)
            res.update(rc=rc, dur=round(dur, 1))
            if rc == 0:
                n = run_sheet_count(runner)
                if session is None:
                    session = new_session()
                    log(cfg, f"SESSION OPEN — {session['session_id']}")
                session["runs"].append(os.path.basename(runner or ""))
                session["sheets"] += n
                session["last_activity"] = time.time()
                write_session(cfg, session)
                res.update(sheets=n, run=runner)
                log(cfg, f"MANUAL CAPTURE rc=0 dur={dur:.1f}s run={runner} "
                         f"sheets={n} (session {session['session_id']}: "
                         f"{session['sheets']} sheet(s) / {len(session['runs'])} run(s))")
            elif rc == 3:
                res.update(note="feeder empty — insert a sheet and scan again")
                log(cfg, "MANUAL scan: feeder empty")
            elif rc == 2:
                res.update(note="scanner busy — try again")
                log(cfg, "MANUAL scan: scanner busy")
            else:
                res.update(note=tail or f"rc={rc}")
                log(cfg, f"MANUAL scan: rc={rc} {tail}")
    elif act == "finish":
        if session is not None:
            closed = close_session(cfg, session, "user")
            res.update(rc=0, sheets=closed["sheets"],
                       note=f"session closed — {closed['sheets']} sheet(s) in "
                            f"{len(closed['runs'])} run(s)")
            session = None
        else:
            res.update(rc=None, note="no open session")
            log(cfg, "MANUAL finish: no open session")
    else:
        res.update(rc=None, note=f"unknown action {act!r}")
    return session, res, closed


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
             f"ping={cfg['ping_interval']}s; scan={cfg['scan_interval']}s; "
             f"out={cfg['scan_out']})")

    # crash recovery: a session left open by a previous run is closed now
    session = None
    st = read_state(cfg)
    orphan = st.get("session")
    if isinstance(orphan, dict) and orphan.get("open"):
        sess = dict(orphan)
        sess.setdefault("runs", [])
        sess.setdefault("sheets", 0)
        sess.setdefault("started_ts", time.time())
        closed = close_session(cfg, sess, "restart (orphan)")
        log(cfg, "closed orphan session left open by a previous run")
        write_state(cfg, mode="watching", session=None,
                    last_session=last_session_state(closed))
    else:
        write_state(cfg, mode="watching", session=None)

    online = False
    suspended = False
    busy_streak = err_streak = empty_streak = 0
    partial_alerted = False
    captures = 0
    pending = None
    next_attempt_at = time.time()
    next_ping_at = time.time() + cfg["ping_interval"]

    def state_session_and_mode():
        if suspended:
            m = "suspended"
        elif online:
            m = "online"
        else:
            m = "offline"
        return m

    while not STOP:
        # ---- 1) handle a pending request (from a button or this loop) -------
        if pending is None:
            pending = read_request(cfg)
        if pending is not None:
            req, pending = pending, None
            session, res, closed = handle_request(cfg, req, session, online, suspended)
            if res.get("action") == "scan" and res.get("rc") == 0:
                captures += 1
            kwargs = {}
            if session is None:
                # nothing open: let ambient resume gently after a manual action
                next_attempt_at = time.time() + cfg["capture_cooldown"]
                if closed is not None:
                    kwargs["last_session"] = last_session_state(closed)
            write_state(cfg, mode=state_session_and_mode(),
                        session=session_state(session), last_request=res,
                        captures=captures, **kwargs)
            continue

        # ---- 2) offline / suspended ----------------------------------------
        if not online:
            if suspended:
                if not ping_ok(cfg["host"]):
                    suspended = False
                    busy_streak = 0
                    log(cfg, "device offline while suspended — resume normal watch")
                    write_state(cfg, mode="offline")
                else:
                    pending = wait_check(cfg, cfg["ping_interval"])
                    continue
            if not ping_ok(cfg["host"]):
                pending = wait_check(cfg, cfg["ping_interval"])
                continue
            online = True
            suspended = False
            write_state(cfg, mode="online")
            log(cfg, f"scanner ONLINE — first scan attempt in {cfg['scan_interval']}s")
            pending = wait_check(cfg, cfg["scan_interval"])
            if STOP:
                break
            if pending is not None:
                continue   # a request came in — handled at the top (user vouches)
            if not ping_ok(cfg["host"]):
                online = False
                log(cfg, "scanner went offline during the settle wait — back to watch")
                write_state(cfg, mode="offline")
                continue

        # ---- 3) open session: ambient paused, watch for requests/timeout ----
        if session is not None:
            if time.time() - session["last_activity"] > cfg["session_idle_close"]:
                closed = close_session(cfg, session, "idle timeout")
                session = None
                write_state(cfg, session=None, last_session=last_session_state(closed))
                next_attempt_at = time.time() + cfg["capture_cooldown"]
                continue
            if time.time() >= next_ping_at:
                next_ping_at = time.time() + cfg["ping_interval"]
                if not ping_ok(cfg["host"]):
                    closed = close_session(cfg, session, "scanner offline")
                    session = None
                    online = False
                    busy_streak = err_streak = empty_streak = 0
                    write_state(cfg, mode="offline", session=None,
                                last_session=last_session_state(closed))
                    log(cfg, "scanner went offline — pausing attempts")
                    continue
            pending = wait_check(cfg, cfg["request_check_interval"])
            continue

        # ---- 4) ambient attempts (user-paced mode off) ----------------------
        if time.time() < next_attempt_at:
            pending = wait_check(cfg, min(next_attempt_at - time.time(),
                                          cfg["request_check_interval"]))
            continue

        rc, dur, runner, tail = attempt(cfg)
        if rc == 0:
            captures += 1
            empty_streak = busy_streak = err_streak = 0
            partial_alerted = False
            log(cfg, f"CAPTURED rc=0 dur={dur:.1f}s run={runner} (session captures: {captures})")
            write_state(cfg, mode="online", last_rc=rc, last_dur=round(dur, 1),
                        last_run=runner, captures=captures)
            next_attempt_at = time.time() + cfg["capture_cooldown"]
        elif rc == 3:
            empty_streak += 1
            busy_streak = err_streak = 0
            iv = cfg["idle_interval"] if empty_streak >= cfg["idle_stretch_after"] else cfg["scan_interval"]
            log(cfg, f"empty rc=3 dur={dur:.1f}s (empty streak {empty_streak}; next in {iv}s)")
            write_state(cfg, mode="online", last_rc=rc, last_dur=round(dur, 1),
                        empty_streak=empty_streak)
            next_attempt_at = time.time() + iv
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
                pending = wait_check(cfg, 60)
            else:
                write_state(cfg, mode="online", last_rc=rc, busy_streak=busy_streak)
                next_attempt_at = time.time() + cfg["busy_backoff"]
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
                pending = wait_check(cfg, cfg["ping_interval"])
            else:
                next_attempt_at = time.time() + cfg["scan_interval"]

    if session is not None:
        closed = close_session(cfg, session, "scan-watch stopping")
        log(cfg, "scan-watch stopping (signal)")
        write_state(cfg, mode="stopped", session=None,
                    last_session=last_session_state(closed))
    else:
        log(cfg, "scan-watch stopping (signal)")
        write_state(cfg, mode="stopped")


if __name__ == "__main__":
    main()
