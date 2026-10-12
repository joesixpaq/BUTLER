#!/usr/bin/env python3
"""Automated regression for the manual session flow — scan-watch + intake + board.
Runs WITHOUT the scanner (fake pull script + pingable localhost); no hardware.

    python3 deploy/scanwatch-selftest/session-test.py

Covers:
  s1  request -> capture -> session opens -> second capture extends -> finish closes
  s2  no automatic scans — ICMP watch only (no attempts without requests)
  s3  orphan recovery: kill -9 with an open session -> restart closes it
  s4  idle timeout closes an abandoned session
  s5  scan request while the scanner is offline -> polite "offline" result
  B   intake: closed session = one batch (+merged PDF), member runs skipped,
      open session held, stale-open registered, idempotent re-ingest,
      settle window + duplicate self-heal (membership race guard)
  C   board API: scanner status, scan request file, offline refusal, trash
      (archive files + mark batch + hide from board + banner on sheet page)
"""
import json
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import time

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
SW_PY = REPO / "deploy" / "scan-watch.py"
FAKE = HERE / "fake-scan.py"

FAILED = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"   [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def write_json(p, obj):
    p = pathlib.Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=1))


def read_json(p, default=None):
    try:
        return json.loads(pathlib.Path(p).read_text())
    except Exception:
        return default


class SW:
    """A scan-watch test instance in its own tmp dir."""

    def __init__(self, tmp, plan, **over):
        self.tmp = pathlib.Path(tmp)
        (self.tmp / "scans/direct").mkdir(parents=True, exist_ok=True)
        (self.tmp / "scans/sessions").mkdir(parents=True, exist_ok=True)
        write_json(self.tmp / "plan.json", plan)
        self.cfg = {
            "host": "127.0.0.1", "pull_script": str(FAKE),
            "scan_out": str(self.tmp / "scans/direct"),
            "ping_interval": 2, "attempt_timeout": 30,
            "log_file": str(self.tmp / "log.txt"), "state_file": str(self.tmp / "state.json"),
            "request_file": str(self.tmp / "scan-request.json"),
            "sessions_dir": str(self.tmp / "scans/sessions"),
            "request_check_interval": 1, "session_idle_close": 60,
            "duplex": True, "mode": "color",
        }
        self.cfg.update(over)
        self.cfg_path = self.tmp / "cfg.json"
        write_json(self.cfg_path, self.cfg)
        self.env = {**os.environ, "SWTEST_PLAN": str(self.tmp / "plan.json"),
                    "SWTEST_N": str(self.tmp / "n")}
        self.proc = None

    def start(self):
        self.proc = subprocess.Popen(
            [sys.executable, str(SW_PY), "--config", str(self.cfg_path)],
            env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return self

    def stop(self, sig=signal.SIGTERM):
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(sig)
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def state(self):
        return read_json(self.tmp / "state.json", {}) or {}

    def log(self):
        try:
            return pathlib.Path(self.cfg["log_file"]).read_text()
        except Exception:
            return ""

    def attempts(self):
        p = self.tmp / "n"
        return int(p.read_text().strip()) if p.exists() else 0

    def request(self, action):
        write_json(self.cfg["request_file"],
                   {"action": action, "id": f"t{time.time():.0f}", "ts": time.time()})

    def sessions(self):
        d = pathlib.Path(self.cfg["sessions_dir"])
        return [read_json(p) for p in sorted(d.glob("*.json"))] if d.exists() else []

    def wait_for(self, fn, timeout=40, step=0.3):
        end = time.time() + timeout
        while time.time() < end:
            try:
                v = fn()
            except Exception:
                v = None
            if v:
                return v
            time.sleep(step)
        return None


def s1_session_flow():
    print("--- s1: capture / extend / finish ---")
    sw = SW(tempfile.mkdtemp(prefix="sw1_"), ["capture", "capture"])
    sw.start()
    check("s1a scanner online", sw.wait_for(lambda: sw.state().get("mode") == "online"))
    sw.request("scan")
    check("s1b first capture opens session (2 sheets)",
          sw.wait_for(lambda: bool(sw.sessions()) and sw.sessions()[0].get("sheets") == 2),
          str(sw.sessions()))
    sw.request("scan")
    check("s1c second capture extends session",
          sw.wait_for(lambda: len(sw.sessions()[0].get("runs", [])) == 2))
    sw.request("finish")
    check("s1d finish closes session",
          sw.wait_for(lambda: bool(sw.sessions()) and sw.sessions()[0].get("closed")),
          str(sw.sessions()))
    s = sw.sessions()[0]
    check("s1e closed reason = user", s.get("closed_reason") == "user")
    check("s1f 4 sheets in 2 runs", s.get("sheets") == 4 and len(s.get("runs", [])) == 2)
    st = sw.state()
    check("s1g last_request shows close", "session closed" in (st.get("last_request") or {}).get("note", ""))
    check("s1h last_session recorded", (st.get("last_session") or {}).get("sheets") == 4)
    check("s1i exactly 2 device attempts (no ambient)", sw.attempts() == 2, f"n={sw.attempts()}")
    sw.stop()


def s2_no_ambient():
    print("--- s2: no automatic scans — ICMP watch only ---")
    sw = SW(tempfile.mkdtemp(prefix="sw2_"), ["capture", "capture", "empty", "empty"])
    sw.start()
    check("s2a scanner online (ICMP)", sw.wait_for(lambda: sw.state().get("mode") == "online"))
    time.sleep(8)
    check("s2b no attempts while idle (no requests)", sw.attempts() == 0, f"n={sw.attempts()}")
    sw.request("scan")
    check("s2c request works", sw.wait_for(lambda: bool(sw.sessions()) and sw.sessions()[0].get("sheets") == 2))
    sw.request("finish")
    check("s2d finish closes", sw.wait_for(lambda: bool(sw.sessions()) and sw.sessions()[0].get("closed")))
    time.sleep(5)
    check("s2e still no spontaneous attempts after close", sw.attempts() == 1, f"n={sw.attempts()}")
    sw.stop()


def s3_orphan_recovery():
    print("--- s3: kill -9 with open session -> restart closes orphan ---")
    tmp = tempfile.mkdtemp(prefix="sw3_")
    sw = SW(tmp, ["capture"])
    sw.start()
    check("s3a online", sw.wait_for(lambda: sw.state().get("mode") == "online"))
    sw.request("scan")
    check("s3b session open", sw.wait_for(lambda: bool(sw.sessions()) and sw.sessions()[0].get("sheets") == 2))
    sw.stop(sig=signal.SIGKILL)
    sw2 = SW(tmp, ["empty", "empty", "empty"])
    sw2.start()
    ok = sw2.wait_for(lambda: bool(sw2.sessions()) and sw2.sessions()[0].get("closed_reason") == "restart (orphan)")
    check("s3c orphan closed on restart", ok, str(sw2.sessions()))
    sw2.stop()


def s4_idle_timeout():
    print("--- s4: idle timeout closes abandoned session ---")
    sw = SW(tempfile.mkdtemp(prefix="sw4_"), ["capture", "empty", "empty", "empty"],
           session_idle_close=6)
    sw.start()
    check("s4a online", sw.wait_for(lambda: sw.state().get("mode") == "online"))
    sw.request("scan")
    check("s4b session open", sw.wait_for(lambda: bool(sw.sessions()) and sw.sessions()[0].get("sheets") == 2))
    ok = sw.wait_for(lambda: bool(sw.sessions()) and sw.sessions()[0].get("closed_reason") == "idle timeout",
                     timeout=25)
    check("s4c idle timeout closed it", ok, str(sw.sessions()))
    sw.stop()


def s5_offline_request():
    print("--- s5: scan request while scanner offline ---")
    sw = SW(tempfile.mkdtemp(prefix="sw5_"), ["capture"], host="192.0.2.1")
    sw.start()
    time.sleep(4)
    sw.request("scan")
    ok = sw.wait_for(lambda: (sw.state().get("last_request") or {}).get("note") == "scanner offline", timeout=20)
    check("s5a offline note returned", ok, str(sw.state().get("last_request")))
    check("s5b no device attempt made", sw.attempts() == 0)
    sw.stop()


def phase_b_intake():
    print("--- B: intake session registration ---")
    sys.path.insert(0, str(REPO))
    import butler.db as bdb
    import butler.intake as bintake

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="butler_intake_"))
    direct = tmp / "scans/direct"
    sessions = tmp / "scans/sessions"
    direct.mkdir(parents=True)
    sessions.mkdir(parents=True)
    bdb.DB_PATH = tmp / "butler.db"
    bintake.SCAN_DIRS = [direct, tmp / "scans/incoming"]
    bintake.SESSIONS_DIR = sessions
    bdb.init()

    def mk_run(name, sheets=1, with_pdf=False, age=120):
        d = direct / name
        (d / "sheets").mkdir(parents=True)
        sh = []
        for i in range(1, sheets + 1):
            fn = f"sheet_{i:02d}_front.jpg"
            (d / "sheets" / fn).write_bytes(b"\xff\xd8\xff\xd9")
            sh.append({"index": i, "front": f"sheets/{fn}", "back": ""})
        write_json(d / "manifest.json",
                   {"scan_timestamp": name.split("_", 1)[1], "sheets": sh,
                    "stream_end": "done-marker"})
        # backdate so runs are past the intake settle window by default
        # (age=0 simulates a just-finished run)
        t = time.time() - age
        os.utime(d / "manifest.json", (t, t))
        if with_pdf:
            import fitz
            doc = fitz.open()
            doc.new_page()
            doc.save(d / f"scan_{name}.pdf")
            doc.close()
        return d

    now = time.time()
    mk_run("pull_20261012_090001", sheets=2, with_pdf=True)
    mk_run("pull_20261012_090030", sheets=1, with_pdf=True)
    mk_run("pull_20261012_090100", sheets=2)          # open session B member
    mk_run("pull_20261012_090200", sheets=1)          # standalone
    mk_run("pull_20261012_090300", sheets=1)          # stale session C member
    write_json(sessions / "20261012_090000.json",
               {"session_id": "20261012_090000", "started": "2026-10-12T09:00:00",
                "started_ts": now - 60, "closed": "2026-10-12T09:01:00",
                "closed_reason": "user", "source": "manual",
                "runs": ["pull_20261012_090001", "pull_20261012_090030"], "sheets": 3})
    write_json(sessions / "20261012_090050.json",
               {"session_id": "20261012_090050", "started": "2026-10-12T09:00:50",
                "started_ts": now - 20, "closed": None, "source": "manual",
                "runs": ["pull_20261012_090100"], "sheets": 2})
    write_json(sessions / "20261012_090010.json",
               {"session_id": "20261012_090010", "started": "2026-10-11T09:00:10",
                "started_ts": now - 25 * 3600, "closed": None, "source": "manual",
                "runs": ["pull_20261012_090300"], "sheets": 1})

    new, _ = bintake.ingest(verbose=True)
    keys = {k for k, _ in new}
    check("B1 closed session registered", "sessions/20261012_090000" in keys, str(keys))
    check("B2 standalone run registered", "direct/pull_20261012_090200" in keys)
    check("B3 open session NOT registered", "sessions/20261012_090050" not in keys)
    check("B4 open session's run skipped", "direct/pull_20261012_090100" not in keys)
    check("B5 stale-open session registered", "sessions/20261012_090010" in keys)
    check("B6 closed session's runs skipped",
          "direct/pull_20261012_090001" not in keys and "direct/pull_20261012_090030" not in keys)

    con = bdb.connect()
    row = con.execute("SELECT * FROM batches WHERE key='sessions/20261012_090000'").fetchone()
    nn = con.execute("SELECT COUNT(*) FROM sheets WHERE batch_id=?", (row["id"],)).fetchone()[0] if row else 0
    check("B7 session batch has 3 sheets", nn == 3, f"sheets={nn}")
    fronts = [r["front"] for r in con.execute(
        "SELECT front FROM sheets WHERE batch_id=? ORDER BY sheet_no", (row["id"],))]
    check("B8 sheet paths prefixed with run",
          fronts[:1] == ["pull_20261012_090001/sheets/sheet_01_front.jpg"], str(fronts))
    check("B9 merged session PDF exists", (direct / "scan_session_20261012_090000.pdf").exists())
    note = con.execute("SELECT note FROM batches WHERE key='sessions/20261012_090010'").fetchone()["note"]
    check("B10 stale note present", "stale-open" in note, note)
    again, _ = bintake.ingest()
    check("B11 idempotent re-ingest", again == [], str(again))
    con.close()

    # membership race guard (live bug 2026-10-12 23:38): the settle window,
    # the fresh membership re-read, and the duplicate self-heal
    mk_run("pull_20261012_090400", sheets=1, age=0)
    new2, _ = bintake.ingest()
    check("B12 fresh run waits out the settle window",
          "direct/pull_20261012_090400" not in {k for k, _ in new2})
    t = time.time() - 120
    os.utime(direct / "pull_20261012_090400" / "manifest.json", (t, t))
    new2, _ = bintake.ingest()
    check("B13 settled run registers",
          "direct/pull_20261012_090400" in {k for k, _ in new2})

    mk_run("pull_20261012_090500", sheets=1)      # settled, no session yet
    new2, _ = bintake.ingest()
    check("B14 race sim: standalone registered first",
          "direct/pull_20261012_090500" in {k for k, _ in new2})
    write_json(sessions / "20261012_090060.json",
               {"session_id": "20261012_090060", "started": "2026-10-12T09:00:55",
                "started_ts": now - 40, "closed": "2026-10-12T09:01:10",
                "closed_reason": "user", "source": "manual",
                "runs": ["pull_20261012_090500"], "sheets": 1})
    new2, _ = bintake.ingest()
    con2 = bdb.connect()
    st = con2.execute("SELECT status,note FROM batches WHERE "
                      "key='direct/pull_20261012_090500'").fetchone()
    con2.close()
    check("B15 session registers; duplicate auto-retired",
          "sessions/20261012_090060" in {k for k, _ in new2}
          and st["status"] == "trashed" and "duplicate" in (st["note"] or ""),
          f"{st['status']} / {st['note']}")
    check("B16 files untouched (registry-only retire)",
          (direct / "pull_20261012_090500" / "manifest.json").exists())
    return tmp


def phase_c_web(tmp):
    print("--- C: board API / trash ---")
    sys.path.insert(0, str(REPO))
    import butler.db as bdb
    write_json(tmp / "state.json", {
        "mode": "online",
        "session": {"open": True, "session_id": "20261012_090050", "sheets": 2,
                    "runs": ["pull_20261012_090100"], "started": "2026-10-12T09:00:50"},
        "last_request": {"ts": time.time(), "note": "ok", "rc": 0,
                         "action": "scan", "id": "a"}})
    os.environ.update(BUTLER_SW_STATE=str(tmp / "state.json"),
                      BUTLER_SW_REQUEST=str(tmp / "req.json"),
                      BUTLER_SW_SESSIONS=str(tmp / "scans/sessions"),
                      BUTLER_SCANS_DIR=str(tmp / "scans"))
    import butler.web as bweb
    client = bweb.app.test_client()

    d = client.get("/api/scanner").get_json()
    check("C1 scanner online + session open", d and d["online"] and d["session"]["open"], str(d))
    r = client.post("/api/scan", json={"action": "scan"})
    check("C2 scan request file written",
          r.status_code == 200 and read_json(tmp / "req.json", {}).get("action") == "scan")
    write_json(tmp / "state.json", {"mode": "offline"})
    r = client.post("/api/scan", json={"action": "scan"})
    check("C3 offline scan refused (409)", r.status_code == 409)
    r = client.post("/api/scan", json={"action": "finish"})
    check("C4 finish allowed offline", r.status_code == 200)
    write_json(tmp / "state.json", {"mode": "online"})

    con = bdb.connect()
    bid = con.execute("SELECT id FROM batches WHERE key='sessions/20261012_090010'").fetchone()["id"]
    sid = con.execute("SELECT id FROM sheets WHERE batch_id=? ORDER BY sheet_no", (bid,)).fetchone()["id"]
    con.close()
    r = client.post(f"/api/batch/{bid}/trash")
    moved = (r.get_json() or {}).get("moved", [])
    check("C5 trash ok", r.status_code == 200, str(r.status_code))
    check("C6 run folder archived",
          any("pull_20261012_090300" in m for m in moved) and not (tmp / "scans/direct/pull_20261012_090300").exists(),
          str(moved))
    check("C7 session json archived", not (tmp / "scans/sessions/20261012_090010.json").exists())
    con = bdb.connect()
    st = con.execute("SELECT status FROM batches WHERE id=?", (bid,)).fetchone()["status"]
    con.close()
    check("C8 batch marked trashed", st == "trashed")
    html = client.get("/").get_data(as_text=True)
    check("C9 board hides trashed batch", "20261012_090010" not in html)
    html2 = client.get(f"/sheet/{sid}").get_data(as_text=True)
    check("C10 trashed banner on sheet page", "was trashed" in html2)


def main():
    t0 = time.time()
    s1_session_flow()
    s2_no_ambient()
    s3_orphan_recovery()
    s4_idle_timeout()
    s5_offline_request()
    tmp_b = phase_b_intake()
    phase_c_web(tmp_b)
    print()
    print(f"{'ALL PASS' if not FAILED else 'FAILURES: ' + ', '.join(FAILED)}  ({time.time() - t0:.0f}s)")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
