#!/usr/bin/env python3
"""tabc status over HTTP: exit codes, and the query writes nothing.

Contract:
- Against this daemon: exit 0 with the counts and the oldest unopened message, whether or not
  anything is unread. The ledger is the same before and after, mailbox_opens included.
- /status answers only for the signing node (403 for another node).
- A daemon without /status (404 not found): exit 2, reported as failed, never as zero unread.
- HTTP 400 / 401 / 403: exit 2. The same for send --check (jack 48854bab N1).
- Nothing listening: exit 1.
- With TABC_OLD_ROOT set to the source tree of an older release: exit 2 against that real daemon.
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

KEYDIR = tempfile.mkdtemp(prefix="ibsh_keys_")
os.environ["TABC_HOME"] = KEYDIR
sys.path.insert(0, ROOT)
from tabus import nodekey  # noqa: E402

FAILS = []


def check(name, ok, detail=""):
    if not ok:
        FAILS.append(name)
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""))


def call(base, method, path, payload, acting):
    body = json.dumps(payload, ensure_ascii=False) if payload is not None else ""
    req = urllib.request.Request(base + path, method=method)
    data = None
    if payload is not None:
        data = body.encode("utf-8")
        req.add_header("Content-Type", "application/json; charset=utf-8")
    ts = str(int(time.time()))
    sig = nodekey.b58encode(
        nodekey.sign(nodekey.canonical_request(acting, method, path, body, ts), nodekey.key_path(acting))
    )
    req.add_header("X-Node", acting)
    req.add_header("X-Node-Ts", ts)
    req.add_header("X-Node-Sig", sig)
    try:
        with urllib.request.urlopen(req, data=data, timeout=35) as res:
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
    env = dict(os.environ, TABC_BUS_URL=base, TABC_NODE="erin")
    env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "tabus.cli", *args], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60
    )


def ledger(dbpath):
    c = sqlite3.connect(dbpath)
    try:
        out = {}
        for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
            out[t] = sorted(tuple(r) for r in c.execute(f'SELECT * FROM "{t}"'))
        return out
    finally:
        c.close()


def against(root, label):
    tmpdir = tempfile.mkdtemp(prefix=f"ibsh_{label}_")
    dbpath = os.path.join(tmpdir, "t.db")
    proc, _port, base, _log = server_harness.start(root, tmpdir, dbpath)
    atexit.register(server_harness.stop, proc)
    try:
        for n in ("erin", "gus"):
            code, r = register(base, n)
            check(f"{label}: register {n}", code == 200 and r.get("ok"), str(r)[:160])
        return proc, base, dbpath
    except BaseException:
        server_harness.stop(proc)
        raise


STATUS = ["status", "--node", "erin"]
CHECK = ["send", "--sender", "erin", "--to", "gus", "--subject", "s", "--body", "b", "--check"]

# 1. This daemon.
proc, base, dbpath = against(ROOT, "new")
try:
    before = ledger(dbpath)
    r = cli(base, *STATUS)
    check("new: empty inbox exits 0", r.returncode == 0 and "unread 0" in r.stdout and "no send is blocked" in r.stdout,
          r.stdout + r.stderr)
    check("new: status wrote nothing (mailbox_opens included)", ledger(dbpath) == before)
    code, _ = call(base, "POST", "/send", {"from": "gus", "to": ["erin"], "subject": "from-gus", "body": "x"}, "gus")
    check("new: gus sends erin a DM", code == 200)
    before = ledger(dbpath)
    r = cli(base, *STATUS)
    check(
        "new: unread exits 0, shows the oldest and the blocked target",
        r.returncode == 0 and "not opened 1" in r.stdout and "from-gus" in r.stdout and "dm to gus (1)" in r.stdout,
        r.stdout + r.stderr,
    )
    check("new: status with unread wrote nothing", ledger(dbpath) == before)
    code, body = call(base, "GET", "/status?node=gus", None, "erin")
    check("new: another node's status is refused 403", code == 403, f"{code} {body}")
finally:
    server_harness.stop(proc)

# 2. Fake daemons: 404 (no /status, no /send_check) and 403.
for http_code, label in ((404, "404"), (403, "403")):
    seen = []

    class Fake(http.server.BaseHTTPRequestHandler):
        def _answer(self):
            seen.append(self.path)
            n = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(n)
            out = json.dumps({"error": "not found" if http_code == 404 else "forbidden"}).encode()
            self.send_response(http_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        do_GET = do_POST = _answer

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    fake = f"http://127.0.0.1:{srv.server_address[1]}"
    r = cli(fake, *STATUS)
    check(f"{label} daemon: status exits 2, not zero unread", r.returncode == 2 and "status failed" in r.stdout
          and "unread 0" not in r.stdout, r.stdout + r.stderr)
    r = cli(fake, *CHECK)
    check(f"{label} daemon: send --check exits 2", r.returncode == 2 and "check failed" in r.stdout, r.stdout + r.stderr)
    check(f"{label} daemon: /send never called", "/send" not in seen, str(seen))
    srv.shutdown()

# 3. Nothing listening.
dead = f"http://127.0.0.1:{server_harness.free_port()}"
r = cli(dead, *STATUS)
check("no daemon: status exits 1, not zero unread", r.returncode == 1 and "status failed" in r.stdout and "unread 0" not in r.stdout,
      r.stdout + r.stderr)

# 4. A real older daemon, when its source tree is given.
old_root = os.environ.get("TABC_OLD_ROOT")
if old_root:
    proc, base, dbpath = against(old_root, "old")
    try:
        before = ledger(dbpath)
        r = cli(base, *STATUS)
        check("old daemon: status exits 2", r.returncode == 2 and "older than 0.2.4.2" in r.stdout, r.stdout + r.stderr)
        check("old daemon: nothing written", ledger(dbpath) == before)
    finally:
        server_harness.stop(proc)
else:
    print("SKIP old daemon (set TABC_OLD_ROOT to an older source tree)")

print(f"\n=== Results: FAIL {len(FAILS)} ===")
sys.exit(1 if FAILS else 0)
