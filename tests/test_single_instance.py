#!/usr/bin/env python3
"""One doorbell per state directory, a clear refusal for a second tabd, and exit tables that match the code.

Contract:
- A second doorbell on the same TABC_HOME exits 75, leaves the holder running and its PID
  in the lock file, and records nothing. After the holder ends, a new one starts.
- Doorbells on different TABC_HOME directories both start.
- The lock descriptor is not inheritable by child processes.
- An unknown DOORBELL_MODE exits 2 without taking the lock.
- A second tabd on a taken port exits 2 with one line and no traceback, and the store is
  unchanged by it; the first keeps answering.
- Every exit code a file uses appears in that file's header table.
Doorbells run in SHADOW mode only: nothing is typed into any terminal.
"""

import ast
import os
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server_harness  # noqa: E402  — imports nothing from the package at import time

FAILS = []
OWN = []  # every process this test starts; only these are signalled


def check(name, ok, detail=""):
    if not ok:
        FAILS.append(name)
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail and not ok else ""))


def env_for(home, db):
    e = dict(os.environ, TABC_HOME=home, TABC_DB=db, DOORBELL_MODE="SHADOW", DOORBELL_POLL_SEC="1",
             PYTHONPATH=ROOT + os.pathsep + os.environ.get("PYTHONPATH", ""), PYTHONDONTWRITEBYTECODE="1")
    e.pop("DOORBELL_LEDGER", None)
    e.pop("TABC_BUS_URL", None)
    os.makedirs(home, exist_ok=True)
    return e


def bell(env, log):
    p = subprocess.Popen([sys.executable, "-m", "tabus.doorbell"], cwd=os.path.dirname(env["TABC_HOME"]),
                         env=env, stdout=open(log, "w"), stderr=subprocess.STDOUT)
    OWN.append(p)
    return p


def wait_for(log, text, t=10.0):
    end = time.time() + t
    while time.time() < end:
        if os.path.exists(log) and text in open(log).read():
            return True
        time.sleep(0.1)
    return False


def stop(p):
    if p.poll() is None:
        p.send_signal(signal.SIGTERM)
        try:
            return p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill()
            return p.wait(5)
    return p.returncode


def rows(db, table):
    if not os.path.exists(db):
        return None
    c = sqlite3.connect(db)
    try:
        return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except sqlite3.OperationalError:
        return None
    finally:
        c.close()


def ledger(db):
    c = sqlite3.connect(db)
    try:
        out = {}
        for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
            out[t] = sorted(tuple(r) for r in c.execute(f'SELECT * FROM "{t}"'))
        return out
    finally:
        c.close()


tmp = tempfile.mkdtemp(prefix="single_")
try:
    # 1. Two doorbells, same state directory.
    home = os.path.join(tmp, "a", "home")
    env = env_for(home, os.path.join(tmp, "a", "t.db"))
    l1, l2 = os.path.join(tmp, "a1.log"), os.path.join(tmp, "a2.log")
    first = bell(env, l1)
    check("first doorbell starts", wait_for(l1, "started"), open(l1).read() if os.path.exists(l1) else "")
    lock = os.path.join(home, "lock-doorbell")
    check("lock file holds the first PID", open(lock).read().strip() == str(first.pid))
    ledger_db = os.path.join(home, "data", "doorbell_ring.db")
    # The first doorbell prints "started" and then writes its start record. Counting
    # in between read the first one's own record as the second's (a CI failure), so
    # wait for that record before taking the count.
    end = time.time() + 10.0
    while (rows(ledger_db, "starts") or 0) < 1 and time.time() < end:
        time.sleep(0.1)
    before = rows(ledger_db, "starts") if os.path.exists(ledger_db) else None
    check("first doorbell recorded its start", (before or 0) >= 1, f"starts={before}")
    second = bell(env, l2)
    try:
        rc2 = second.wait(10)
    except subprocess.TimeoutExpired:
        rc2 = None  # still running: the lock did not stop it
    out2 = open(l2).read()
    check("second doorbell exits 75", rc2 == 75, f"rc={rc2} {out2}")
    check("second says who holds it", f"pid {first.pid}" in out2 and "not started" in out2, out2)
    check("second did not start a round", "started" not in out2.replace("not started", ""), out2)
    check("holder still running", first.poll() is None)
    check("holder PID kept in the lock file", open(lock).read().strip() == str(first.pid))
    check("second recorded nothing", (rows(ledger_db, "starts") if os.path.exists(ledger_db) else None) == before,
          f"{before}")
    rc1 = stop(first)
    check("holder stops 0 on SIGTERM", rc1 == 0, f"rc={rc1}")
    third = bell(env, os.path.join(tmp, "a3.log"))
    check("after the holder ends, a new one starts", wait_for(os.path.join(tmp, "a3.log"), "started"))
    stop(third)

    # 2. Different state directories do not block each other.
    eb = env_for(os.path.join(tmp, "b", "home"), os.path.join(tmp, "b", "t.db"))
    ec = env_for(os.path.join(tmp, "c", "home"), os.path.join(tmp, "c", "t.db"))
    pb, pc = bell(eb, os.path.join(tmp, "b.log")), bell(ec, os.path.join(tmp, "c.log"))
    check("different TABC_HOME: both start",
          wait_for(os.path.join(tmp, "b.log"), "started") and wait_for(os.path.join(tmp, "c.log"), "started"))
    stop(pb)
    stop(pc)

    # 3. Unknown mode: exit 2, no lock taken.
    ed = env_for(os.path.join(tmp, "d", "home"), os.path.join(tmp, "d", "t.db"))
    ed["DOORBELL_MODE"] = "LOUD"
    pd = bell(ed, os.path.join(tmp, "d.log"))
    rcd = pd.wait(10)
    check("unknown mode exits 2", rcd == 2 and "Traceback" not in open(os.path.join(tmp, "d.log")).read(), f"rc={rcd}")
    check("unknown mode took no lock", not os.path.exists(os.path.join(tmp, "d", "home", "lock-doorbell")))

    # 4. The lock descriptor is not inheritable.
    sys.path.insert(0, ROOT)
    os.environ["TABC_HOME"] = os.path.join(tmp, "e", "home")
    os.makedirs(os.environ["TABC_HOME"])
    from tabus import doorbell  # noqa: E402

    fd, holder = doorbell.acquire_lock(os.environ["TABC_HOME"])
    check("in-process lock acquired", fd is not None and holder is None)
    check("lock descriptor not inheritable", fd is not None and os.get_inheritable(fd) is False)
    fd2, holder2 = doorbell.acquire_lock(os.environ["TABC_HOME"])
    check("same process, second open is refused and names the holder", fd2 is None and holder2 == str(os.getpid()))
    os.close(fd)

    # 5. Second tabd on a taken port.
    tdir = os.path.join(tmp, "t")
    os.makedirs(tdir)
    dbpath = os.path.join(tdir, "t.db")
    proc, port, base, _log = server_harness.start(ROOT, tdir, dbpath)
    OWN.append(proc)
    snap = ledger(dbpath)
    env_t = dict(os.environ, TABC_HOME=os.path.join(tdir, "home"), TABC_DB=dbpath,
                 PYTHONPATH=ROOT + os.pathsep + os.environ.get("PYTHONPATH", ""), PYTHONDONTWRITEBYTECODE="1")
    r = subprocess.run([sys.executable, "-m", "tabus.daemon", "--port", str(port)], cwd=tdir, env=env_t,
                       capture_output=True, text=True, timeout=30)
    out = (r.stdout + r.stderr).strip()
    check("second tabd exits 2", r.returncode == 2, f"rc={r.returncode} {out}")
    check("second tabd: one line, no traceback", "Traceback" not in out and out.count("\n") == 0 and "already in use" in out, out)
    check("second tabd left the store unchanged", ledger(dbpath) == snap)
    # Sharper: point the refused tabd at a store that does not exist yet. Preparing the
    # ledger before the bind would create it; refusing first leaves no file.
    fresh = os.path.join(tdir, "never.db")
    env_f = dict(env_t, TABC_DB=fresh)
    r = subprocess.run([sys.executable, "-m", "tabus.daemon", "--port", str(port)], cwd=tdir, env=env_f,
                       capture_output=True, text=True, timeout=30)
    check("refused tabd created no store", r.returncode == 2 and not os.path.exists(fresh),
          f"rc={r.returncode} exists={os.path.exists(fresh)}")
    try:
        with urllib.request.urlopen(base + "/no-such-path", timeout=5) as res:
            alive = True
    except urllib.error.HTTPError:
        alive = True
    except OSError:
        alive = False
    check("first tabd still answers", alive)
    server_harness.stop(proc)
finally:
    for p in OWN:
        try:
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)
                p.wait(10)
        except Exception:
            pass

# 6. Exit tables match the code.
codes_used = {}
for rel in ("tabus/cli.py", "tabus/daemon.py", "tabus/doorbell.py"):
    src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
    doc = ast.get_docstring(ast.parse(src)) or ""
    table = doc.split("Exit codes", 1)[1] if "Exit codes" in doc else ""
    listed = set(int(m) for m in re.findall(r"^\s+(\d+)\s{2,}", table, re.M))
    consts = dict((k, int(v)) for k, v in re.findall(r"^(EXIT_[A-Z_]+)\s*=\s*(\d+)", src, re.M))
    used = set(int(m) for m in re.findall(r"sys\.exit\((\d+)\)", src))
    used |= set(consts[m] for m in re.findall(r"sys\.exit\((EXIT_[A-Z_]+)\)", src) if m in consts)
    used |= set(int(m) for m in re.findall(r"sys\.exit\(0 if [^)]* else (\d+)\)", src))
    check(f"{rel}: header table present", bool(listed), doc[:80])
    check(f"{rel}: every exit code used is in the table", used <= listed, f"used={sorted(used)} listed={sorted(listed)}")

print(f"\n=== Results: FAIL {len(FAILS)} ===")
sys.exit(1 if FAILS else 0)
