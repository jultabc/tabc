#!/usr/bin/env python3
"""tabc send --check over HTTP: exit codes, and nothing is sent when the daemon cannot check.

Contract:
- Against this daemon: exit 0 when not blocked, 3 when the send would be refused, with the
  blocking messages listed. The check stores no message.
- A daemon without /send_check (404 not found): exit 2, nothing sent, and the CLI never calls /send.
- Nothing listening: exit 1, reported as a failed check, never as "not blocked".
- With TABC_OLD_ROOT set to the source tree of a release before 0.2.5 (no /send_check), the same check against
  that real older daemon: exit 2 and no message stored; and the older CLI against this
  daemon sends and reads a refusal as before.
"""

import atexit
import http.server
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server_harness  # noqa: E402  — imports nothing from the package at import time

KEYDIR = tempfile.mkdtemp(prefix="sckh_keys_")
os.environ["TABC_HOME"] = KEYDIR
sys.path.insert(0, ROOT)
from tabus import nodekey  # noqa: E402

FAILS = []


def check(name, ok, detail=""):
    if not ok:
        FAILS.append(name)
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""))


def call(base, method, path, payload, acting):
    body = json.dumps(payload, ensure_ascii=False)
    req = urllib.request.Request(base + path, method=method)
    req.add_header("Content-Type", "application/json; charset=utf-8")
    ts = str(int(time.time()))
    sig = nodekey.b58encode(
        nodekey.sign(nodekey.canonical_request(acting, method, path, body, ts), nodekey.key_path(acting))
    )
    req.add_header("X-Node", acting)
    req.add_header("X-Node-Ts", ts)
    req.add_header("X-Node-Sig", sig)
    try:
        with urllib.request.urlopen(req, data=body.encode("utf-8"), timeout=35) as res:
            return res.status, json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")
        except ValueError:
            return e.code, {}


def register(base, node):
    pub = nodekey.public_key_b58(nodekey.key_path(node))
    return call(base, "POST", "/register", {"node": node, "kind": "test", "pubkey": pub}, node)


def cli(base, *args):
    env = dict(os.environ, TABC_BUS_URL=base)
    env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "tabus.cli", *args], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60
    )


def messages(dbpath):
    c = sqlite3.connect(dbpath)
    try:
        return c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    finally:
        c.close()


CHECK = ["send", "--sender", "erin", "--to", "gus", "--subject", "s", "--body", "b", "--check"]


def against(root, label):
    tmpdir = tempfile.mkdtemp(prefix=f"sckh_{label}_")
    dbpath = os.path.join(tmpdir, "t.db")
    proc, _port, base, log_path = server_harness.start(root, tmpdir, dbpath)
    atexit.register(server_harness.stop, proc)
    try:
        for n in ("erin", "gus"):
            code, r = register(base, n)
            check(f"{label}: register {n}", code == 200 and r.get("ok"), str(r)[:160])
        return proc, base, dbpath, log_path
    except BaseException:
        server_harness.stop(proc)
        raise


# 1. This daemon.
proc, base, dbpath, log_path = against(ROOT, "new")
try:
    before = messages(dbpath)
    r = cli(base, *CHECK)
    check("new: nothing unread exits 0", r.returncode == 0 and "not blocked" in r.stdout, r.stdout + r.stderr)
    check("new: the check stored nothing", messages(dbpath) == before)
    code, _ = call(base, "POST", "/send", {"from": "gus", "to": ["erin"], "subject": "from-gus", "body": "x"}, "gus")
    check("new: gus sends erin a DM", code == 200)
    before = messages(dbpath)
    r = cli(base, *CHECK)
    check(
        "new: blocked exits 3 and lists the message",
        r.returncode == 3 and "UNREAD_BLOCKED" in r.stdout and "from-gus" in r.stdout,
        r.stdout + r.stderr,
    )
    check("new: the blocked check stored nothing", messages(dbpath) == before)
finally:
    server_harness.stop(proc)


# 2. A daemon that has no /send_check. Every POST answers 404 and is recorded.
seen = []


class Old(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        seen.append(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        out = json.dumps({"error": "not found"}).encode()
        self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    do_GET = do_POST

    def log_message(self, *a):
        pass


srv = http.server.HTTPServer(("127.0.0.1", 0), Old)
threading.Thread(target=srv.serve_forever, daemon=True).start()
fake = f"http://127.0.0.1:{srv.server_address[1]}"
r = cli(fake, *CHECK)
srv.shutdown()
check("404 daemon: exits 2", r.returncode == 2 and "nothing was sent" in r.stdout, r.stdout + r.stderr)
check("404 daemon: /send was never called", "/send" not in seen and "/send_check" in seen, str(seen))

# 3. Nothing listening.
dead = f"http://127.0.0.1:{server_harness.free_port()}"
r = cli(dead, *CHECK)
check("no daemon: exits 1 as a failed check", r.returncode == 1 and "check failed" in r.stdout and "not blocked" not in r.stdout,
      r.stdout + r.stderr)

# 4. A real older daemon, when its source tree is given.
old_root = os.environ.get("TABC_OLD_ROOT")
if old_root:
    proc, base, dbpath, log_path = against(old_root, "old")
    try:
        before = messages(dbpath)
        r = cli(base, *CHECK)
        check("old daemon: exits 2", r.returncode == 2 and "nothing was sent" in r.stdout, r.stdout + r.stderr)
        check("old daemon: no message stored", messages(dbpath) == before, f"{before} -> {messages(dbpath)}")
    finally:
        server_harness.stop(proc)
    # 5. The older CLI against this daemon: the send and its refusal read as before.
    proc, base, dbpath, log_path = against(ROOT, "mixed")
    try:
        def old_cli(*args):
            env = dict(os.environ, TABC_BUS_URL=base)
            env["PYTHONPATH"] = old_root + os.pathsep + env.get("PYTHONPATH", "")
            return subprocess.run([sys.executable, "-m", "tabus.cli", *args], cwd=old_root, env=env,
                                  capture_output=True, text=True, timeout=60)
        r = old_cli("send", "--sender", "gus", "--to", "erin", "--subject", "q", "--body", "x")
        check("mixed: older CLI sends", r.returncode == 0 and "stored id=" in r.stdout, r.stdout + r.stderr)
        r = old_cli("send", "--sender", "erin", "--to", "gus", "--subject", "a", "--body", "x")
        check("mixed: older CLI reads the refusal as before",
              r.returncode == 1 and r.stdout.startswith("failed: read their messages first") and "code UNREAD_BLOCKED" in r.stdout,
              r.stdout + r.stderr)
    finally:
        server_harness.stop(proc)
else:
    print("SKIP old daemon and older CLI (set TABC_OLD_ROOT to an older source tree)")

print(f"\n=== Results: FAIL {len(FAILS)} ===")
sys.exit(1 if FAILS else 0)
