#!/usr/bin/env python3
"""POST /remove answers 503 with JSON when the database is busy, and removes nothing.

bus_remove rolls back and raises on a database error other than a missing table.
The daemon turns that into an answer; without it the client saw the connection
drop with no reply. This test holds a write lock on a real tabd's database.
"""

import atexit
import json
import os
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server_harness  # noqa: E402

KEYDIR = tempfile.mkdtemp(prefix="rmbusy_keys_")
os.environ["TABC_HOME"] = KEYDIR
sys.path.insert(0, ROOT)
from tabus import nodekey  # noqa: E402

FAILS = []


def check(name, ok, detail=""):
    if not ok:
        FAILS.append(name)
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail and not ok else ""))


def call(base, path, payload, acting):
    body = json.dumps(payload, ensure_ascii=False)
    req = urllib.request.Request(base + path, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    ts = str(int(time.time()))
    sig = nodekey.b58encode(nodekey.sign(nodekey.canonical_request(acting, "POST", path, body, ts), nodekey.key_path(acting)))
    req.add_header("X-Node", acting)
    req.add_header("X-Node-Ts", ts)
    req.add_header("X-Node-Sig", sig)
    try:
        with urllib.request.urlopen(req, data=body.encode("utf-8"), timeout=60) as res:
            return res.status, json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")


tmpdir = tempfile.mkdtemp(prefix="rmbusy_")
dbpath = os.path.join(tmpdir, "t.db")
proc, _port, base, _log = server_harness.start(ROOT, tmpdir, dbpath)
atexit.register(server_harness.stop, proc)

for node in ("ops", "zed"):
    nodekey.ensure_key(nodekey.key_path(node))
    pub = nodekey.public_key_b58(nodekey.key_path(node))
    status, out = call(base, "/register", {"node": node, "kind": "test", "pubkey": pub}, node)
    check(f"register {node}", status == 200, f"{status} {out}")

lock = sqlite3.connect(dbpath, timeout=1, isolation_level=None)
lock.execute("BEGIN IMMEDIATE")  # holds the write lock; tabd's writes wait, then fail
try:
    status, out = call(base, "/remove", {"node": "zed", "purge": True, "by": "test"}, "ops")
finally:
    lock.execute("ROLLBACK")
    lock.close()
check("locked database answers 503 with JSON", status == 503 and out.get("ok") is False, f"{status} {out}")
check("the answer says nothing was removed", "nothing was removed" in out.get("msg", ""), out.get("msg", ""))
c = sqlite3.connect(dbpath)
check("node row still there", c.execute("SELECT 1 FROM nodes WHERE node_id='zed'").fetchone() is not None)
check("no removal recorded", c.execute("SELECT COUNT(*) FROM removals WHERE node_id='zed'").fetchone()[0] == 0)
c.close()

print("ALL PASS" if not FAILS else f"FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
