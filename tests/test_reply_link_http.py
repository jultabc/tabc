#!/usr/bin/env python3
"""reply links over HTTP and the CLI: --re, the parent line on read and open, tabc thread.

Contract:
- tabc send --re <prefix> stores a reply; tabc read and tabc open print a `re` line with the
  parent's short id and subject.
- A reply to a message the sender never sent or received fails with exit 1 and code
  REPLY_TARGET_UNAVAILABLE. Unknown and unseen give the same output; only the daemon log says
  which, and the HTTP response never does.
- tabc send --check --re on such a message exits 3 and stores nothing.
- tabc thread shows the thread with replies indented; for a message the node cannot see it exits 2.
"""

import atexit
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server_harness  # noqa: E402

KEYDIR = tempfile.mkdtemp(prefix="rplh_keys_")
os.environ["TABC_HOME"] = KEYDIR
sys.path.insert(0, ROOT)
from tabus import nodekey  # noqa: E402
from tabus import cli as tcli  # noqa: E402
import json  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402


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

FAILS = []


def check(name, ok, detail=""):
    if not ok:
        FAILS.append(name)
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail and not ok else ""))


def cli(*args):
    env = dict(os.environ, TABC_BUS_URL=base)
    env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "tabus.cli", *args], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60
    )


def count(sql):
    c = sqlite3.connect(dbpath)
    try:
        return c.execute(sql).fetchone()[0]
    finally:
        c.close()


def stored_id(r):
    m = re.search(r"stored id=(\S+)", r.stdout)
    return m.group(1) if m else None


tmpdir = tempfile.mkdtemp(prefix="rplh_")
dbpath = os.path.join(tmpdir, "t.db")
proc, _port, base, log_path = server_harness.start(ROOT, tmpdir, dbpath)
atexit.register(server_harness.stop, proc)
try:
    for n in ("ann", "bo", "cy"):
        code, r = register(base, n)
        check(f"register {n}", code == 200 and r.get("ok"), str(r)[:160])

    r = cli("send", "--sender", "ann", "--to", "bo", "--subject", "review request", "--body", "please look")
    root = stored_id(r)
    check("root stored", root is not None, r.stdout + r.stderr)

    r = cli("read", "--node", "bo")
    check("root read has no re line", "please look" in r.stdout and "\nre " not in r.stdout, r.stdout)

    r = cli("send", "--sender", "bo", "--to", "ann", "--subject", "v1 CHANGES", "--body", "fix x", "--re", root[:8])
    reply = stored_id(r)
    check("reply by 8-char prefix stored", reply is not None and r.returncode == 0, r.stdout + r.stderr)

    r = cli("open", "--node", "ann", "--id", reply)
    want = f"re      {root[:tcli.ID_PREFIX_LEN]} review request"
    check("open prints the parent line", want in r.stdout, r.stdout)
    r = cli("read", "--node", "ann")
    check("read prints the parent line", want in r.stdout and "fix x" in r.stdout, r.stdout)

    # Unseen and unknown: same output, the log alone tells them apart.
    n0 = count("SELECT count(*) FROM messages")
    unknown = "bbbbbbbb-0000-4000-8000-000000000009"
    r1 = cli("send", "--sender", "cy", "--to", "ann", "--subject", "s", "--body", "b", "--re", root)
    r2 = cli("send", "--sender", "cy", "--to", "ann", "--subject", "s", "--body", "b", "--re", unknown)
    check("unseen exits 1", r1.returncode == 1 and "REPLY_TARGET_UNAVAILABLE" in r1.stdout, r1.stdout)
    check("unknown exits 1", r2.returncode == 1 and "REPLY_TARGET_UNAVAILABLE" in r2.stdout, r2.stdout)
    check("same output shape", r1.stdout.replace(root, "X") == r2.stdout.replace(unknown, "X"), (r1.stdout, r2.stdout))
    check("nothing stored", count("SELECT count(*) FROM messages") == n0)
    code, body = call(base, "POST", "/send", {"from": "cy", "to": ["ann"], "subject": "s", "body": "b", "reply_to": root}, "cy")
    check("HTTP 400 with the code", code == 400 and body.get("code") == "REPLY_TARGET_UNAVAILABLE", body)
    check("HTTP body never says unseen", "unseen" not in repr(body) and "internal" not in body, body)
    time.sleep(0.3)
    log = open(log_path, encoding="utf-8", errors="replace").read()
    check("log: unseen", f"node=cy target={root} reason=unseen" in log, log[-600:])
    check("log: unknown", f"node=cy target={unknown} reason=unknown" in log, log[-600:])

    r = cli("send", "--sender", "cy", "--to", "ann", "--subject", "s", "--body", "b", "--re", root, "--check")
    check("check exits 3 on unseen", r.returncode == 3, r.stdout + r.stderr)
    check("check stored nothing", count("SELECT count(*) FROM messages") == n0)

    r = cli("send", "--sender", "bo", "--to", "ann", "--subject", "s", "--body", "b", "--re", "abc")
    check("short --re exits 1, REPLY_TARGET_INVALID", r.returncode == 1 and "REPLY_TARGET_INVALID" in r.stdout, r.stdout)

    r = cli("thread", "--node", "bo", root[:8])
    lines = r.stdout.strip().splitlines()
    check("thread exits 0 with two rows", r.returncode == 0 and len(lines) == 2, r.stdout + r.stderr)
    check("thread root unindented, reply indented", lines[:1] and not lines[0].startswith(" ") and lines[1].startswith("  "), lines)
    check("thread row content", "ann → bo" in lines[0] and "review request" in lines[0] and reply[:18] in lines[1], lines)
    r = cli("thread", "--node", "cy", root)
    check("thread on unseen exits 2", r.returncode == 2 and "REPLY_TARGET_UNAVAILABLE" in r.stdout, r.stdout + r.stderr)
finally:
    server_harness.stop(proc)

# Pure helpers.
check(
    "candidate lines",
    tcli._candidate_lines({"code": "REPLY_TARGET_AMBIGUOUS", "details": {"candidates": ["a1", "a2"], "more": 3}})
    == ["candidate a1", "candidate a2", "and 3 more"],
)
check("candidate lines only for ambiguous", tcli._candidate_lines({"code": "UNREAD_BLOCKED", "details": {"candidates": ["a"]}}) == [])
check("reply line hidden parent", tcli._reply_line({"id": "x" * 36}) == "re      " + "x" * 18)
check("reply line none", tcli._reply_line(None) is None)

print()
if FAILS:
    print(f"FAILED {len(FAILS)}: {FAILS}")
    sys.exit(1)
print("all passed")
