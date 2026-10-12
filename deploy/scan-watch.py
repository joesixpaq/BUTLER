#!/usr/bin/env python3
"""scan-watch — ADS-1000W scan grabber for the BUTLER mail pipeline (on-demand).

Behaviour (2026-10-11 rev-2 — user decision: NO automatic scanning)
  ONLINE/OFFLINE: ICMP ping every `ping_interval` (default 90 s; ICMP only —
  never a TCP probe of 54921, which can wedge the device). The board's status
  dot reads the mode from the state file.
  SCANS HAPPEN ONLY ON REQUEST (`request_file`, written by the board's
  "Scan sheet" / "Finish batch" buttons — no other channel):
    - {"action": "scan"}  -> one scan attempt of whatever is in the feeder.
      The first capture OPENS a session; further captures extend it; each
      capture is one immutable pull_<ts> run (membership recorded in
      `sessions_dir`; intake registers the session as ONE batch once closed).
    - {"action": "finish"} -> closes the session.
  Ambient polling was REMOVED at the user's request: the periodic empty-check
  attempts made the device beep every ~2 minutes, and the button is preferred.
  Session close paths: finish / idle timeout / scanner offline / service stop /
  orphan at startup (crash recovery).
    `--once` performs a single scan attempt (troubleshooting).

Safety: single-flight flock (one instance ever); attempts strictly sequential.
        Manual scan requests are executed by THIS process only — never run
        brother-pull-scan by hand while scan-watch might be mid-attempt
        (busy races).
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
    "ping_interval": 90,           # s, ICMP online/offline watch
    "attempt_timeout": 1000,       # s hard cap per attempt (script's own: 900)
    "request_file": os.path.join(HOME, "lan-plan/scan-request.json"),
    "sessions_dir": os.path.join(HOME, "scans/sessions"),
    "request_check_interval": 2,   # s, request-file poll cadence while waiting
    "session_idle_close": 900,     # s, auto-close an idle open session
    "log_file": os.path.join(HOME, "lan-plan/scan-watch.log"),
    "state_file": os.path.join(HOME, "lan-plan/scan-watch.state.json"),
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
# sessions
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


def handle_request(cfg, req, session, online):
    """Execute a board request. Returns (session, result_dict, closed_session)."""
    closed = None
    act = (req or {}).get("action")
    res = {"id": (req or {}).get("id"), "action": act, "ts": time.time()}
    if act == "scan":
        if not online:
            res.update(rc=None, note="scanner offline")
        else:
            rc, dur, runner, tail = attempt(cfg)
            res.update(rc=rc, dur=round(dur, 1))
            n = run_sheet_count(runner)
            if rc == 0 or (rc == 5 and n >= 1):
                # rc=5 = partial capture (jam): the sheets still belong to the
                # feeding session — keep them in the letter and warn (live bug
                # 2026-10-12 23:50: a jammed capture fell out of the session
                # flow and surfaced as its own standalone letter)
                if session is None:
                    session = new_session()
                    log(cfg, f"SESSION OPEN — {session['session_id']}")
                session["runs"].append(os.path.basename(runner or ""))
                session["sheets"] += n
                session["last_activity"] = time.time()
                write_session(cfg, session)
                res.update(sheets=n, run=runner)
                if rc == 5:
                    res.update(note="partial capture — possible jam, check the feeder"
                                    + (f" | {tail}" if tail else ""))
                log(cfg, f"MANUAL CAPTURE rc={rc} dur={dur:.1f}s run={runner} "
                         f"sheets={n} (session {session['session_id']}: "
                         f"{session['sheets']} sheet(s) / {len(session['runs'])} run(s))"
                         + (f" | {tail}" if rc == 5 and tail else ""))
            elif rc == 3:
                res.update(note="feeder empty — insert a sheet and scan again")
                log(cfg, "MANUAL scan: feeder empty")
            elif rc == 2:
                res.update(note="scanner busy — try again")
                log(cfg, "MANUAL scan: scanner busy")
            else:
                note = tail or f"rc={rc}"
                if rc == 5:
                    note = "partial capture — possible jam, check the feeder | " + note
                res.update(note=note)
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
    ap = argparse.ArgumentParser(description="ADS-1000W on-demand scan watcher (BUTLER)")
    ap.add_argument("--config", default=os.path.join(HOME, ".config/butler/scanwatch.json"))
    ap.add_argument("--once", action="store_true", help="single scan attempt then exit (testing)")
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
             f"ping={cfg['ping_interval']}s; request-driven; out={cfg['scan_out']})")

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
    captures = 0
    pending = None
    next_ping_at = time.time() + cfg["ping_interval"]

    while not STOP:
        # ---- 1) handle a pending request (from a button or this loop) -------
        if pending is None:
            pending = read_request(cfg)
        if pending is not None:
            req, pending = pending, None
            session, res, closed = handle_request(cfg, req, session, online)
            if res.get("action") == "scan" and res.get("rc") == 0:
                captures += 1
            kwargs = {}
            if closed is not None:
                kwargs["last_session"] = last_session_state(closed)
            write_state(cfg, mode=("online" if online else "offline"),
                        session=session_state(session), last_request=res,
                        captures=captures, **kwargs)
            continue

        # ---- 2) offline: ICMP watch only ------------------------------------
        if not online:
            if ping_ok(cfg["host"]):
                online = True
                log(cfg, "scanner ONLINE — ICMP watch only; scans happen on request")
                write_state(cfg, mode="online")
            else:
                pending = wait_check(cfg, cfg["ping_interval"])
            continue

        # ---- 3) online: liveness ping + request wait ------------------------
        if time.time() >= next_ping_at:
            next_ping_at = time.time() + cfg["ping_interval"]
            if not ping_ok(cfg["host"]):
                online = False
                if session is not None:
                    closed = close_session(cfg, session, "scanner offline")
                    session = None
                    write_state(cfg, mode="offline", session=None,
                                last_session=last_session_state(closed))
                    log(cfg, "scanner went offline — session closed")
                else:
                    write_state(cfg, mode="offline")
                    log(cfg, "scanner went offline — ICMP watch continues")
                continue
        if session is not None and time.time() - session["last_activity"] > cfg["session_idle_close"]:
            closed = close_session(cfg, session, "idle timeout")
            session = None
            write_state(cfg, session=None, last_session=last_session_state(closed))
            continue
        pending = wait_check(cfg, cfg["request_check_interval"])

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
