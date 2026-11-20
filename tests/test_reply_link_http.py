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
    body = "" if method == "GET" else json.dumps(payload, ensure_ascii=False)
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
        with urllib.request.urlopen(req, data=body.encode("utf-8") if body else None, timeout=35) as res:
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
    check("log: unseen", f"node=cy target='{root}' reason=unseen" in log, log[-600:])
    check("log: unknown", f"node=cy target='{unknown}' reason=unknown" in log, log[-600:])

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
    # Shape: a target with a newline or a percent-encoded newline never reaches the log.
    forged = "cccccccc\n[tabd] reply target unavailable node=ivy target=x reason=unseen"
    code, body = call(base, "POST", "/send", {"from": "bo", "to": ["ann"], "subject": "s", "body": "b", "reply_to": forged}, "bo")
    check("newline target = 400 INVALID", code == 400 and body.get("code") == "REPLY_TARGET_INVALID", body)
    r = cli("thread", "--node", "bo", "cccccccc\nx")
    check("thread with newline id exits 2 INVALID", r.returncode == 2 and "REPLY_TARGET_INVALID" in r.stdout, r.stdout + r.stderr)
    code, body = call(base, "GET", "/thread?node=bo&id=cccccccc%0Areason%3Dunseen", {}, "bo")
    check("/thread %0A id = INVALID", code == 404 and body.get("code") == "REPLY_TARGET_INVALID", body)
    code, body = call(base, "GET", f"/thread?node=bo&id={root}&id={root}", {}, "bo")
    check("/thread repeated key = 400", code == 400, body)
    r = cli("thread", "--node", "bo", root[:8] + "&node=cy")
    check("thread id with & stays one parameter (INVALID, not 403)", r.returncode == 2 and "REPLY_TARGET_INVALID" in r.stdout, r.stdout + r.stderr)
    time.sleep(0.3)
    log = open(log_path, encoding="utf-8", errors="replace").read()
    check("log: no forged line", "target=x reason=unseen" not in log, log[-400:])
    check("log: every refusal line is one line", all(l.startswith("[tabd]") for l in log.splitlines() if "reason=" in l))

    r = cli("thread", "--node", "bo", "--limit", "1", root[:8])
    lines = r.stdout.strip().splitlines()
    check("thread --limit 1 shows one row and the next line", r.returncode == 0 and len(lines) == 2 and lines[1].startswith("… more after this page (1 shown here, 2 in the thread)"), r.stdout + r.stderr)
    nxt = lines[1].split("--after ")[1].split()[0] if len(lines) == 2 and "--after " in lines[1] else ""
    check("next_after is the root", nxt == root, lines)
    r = cli("thread", "--node", "bo", "--limit", "1", "--after", nxt, root[:8])
    lines = r.stdout.strip().splitlines()
    check("second page: the reply, marked parent on another page, no next line", r.returncode == 0 and len(lines) == 1 and f"(re: {root[:tcli.ID_PREFIX_LEN]} on another page)" in lines[0], r.stdout + r.stderr)
    r = cli("thread", "--node", "bo", "--limit", "0", root[:8])
    check("--limit 0 exits 2 THREAD_PAGE_INVALID", r.returncode == 2 and "THREAD_PAGE_INVALID" in r.stdout, r.stdout + r.stderr)
    r = cli("thread", "--node", "bo", "--after", "cccccccc-0000-4000-8000-000000000000", root[:8])
    check("--after not in thread exits 2, not echoed", r.returncode == 2 and "THREAD_PAGE_INVALID" in r.stdout and "cccccccc-0000" not in r.stdout, r.stdout + r.stderr)
    code, body = call(base, "GET", f"/thread?node=bo&id={root}", {}, "bo")
    check("/thread without limit: whole small thread, more false", code == 200 and body.get("total") == 2 and body.get("more") is False and body.get("next_after") is None, body)
    code, body = call(base, "GET", f"/thread?node=bo&id={root}&limit=x1", {}, "bo")
    check("/thread limit not a number = 400", code == 400 and body.get("code") == "THREAD_PAGE_INVALID", body)
    for q in (f"id={root}&after=HUSENTINELAFTER", f"id={root}&limit=HUSENTINELLIMIT",
              f"id={root}&after=HUSENTINEL%0Aline", "id=HUSENTINELID", f"id={root}&limit=1"):
        call(base, "GET", f"/thread?node=bo&{q}", {}, "bo")
    time.sleep(0.3)
    log = open(log_path, encoding="utf-8", errors="replace").read()
    check("access log: no /thread query value", "HUSENTINEL" not in log, [l for l in log.splitlines() if "HUSENTINEL" in l][:3])
    check("access log: /thread lines kept without query", log.count("/thread?<query not logged>") >= 5, log[-400:])
    check("access log: other endpoints unchanged", "POST /send HTTP/1.1" in log, log[-400:])
    for raw in ("%C2%B2", "%D9%A1", "%EF%BC%91"):
        code, body = call(base, "GET", f"/thread?node=bo&id={root}&limit={raw}", {}, "bo")
        check(f"/thread limit {raw} (Unicode digit) = 400", code == 400 and body.get("code") == "THREAD_PAGE_INVALID", body)

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
import datetime as _dt  # noqa: E402
_kst = _dt.timezone(_dt.timedelta(hours=9), "KST")
check("thread time in local zone", tcli._local_minute("2026-11-16T06:36:12+00:00", _kst) == "2026-11-16 15:36 KST")
check("thread time crosses the date", tcli._local_minute("2026-11-16T20:05:00+00:00", _kst) == "2026-11-17 05:05 KST")
check("thread time without zone read as UTC", tcli._local_minute("2026-11-16T06:36:12", _kst) == "2026-11-16 15:36 KST")
check("thread time unparsable kept", tcli._local_minute("not-a-time-at-all", _kst) == "not-a-time-at-al")
check("thread time None", tcli._local_minute(None, _kst) == "")
_row = {"id": "a" * 36, "sender_id": "ann", "subject": "s", "accepted_at": "2026-11-16T06:36:12+00:00", "to": {"nodes": ["bo"]}, "depth": 1}
from tabus import reply_link as _rl  # noqa: E402
_rows = [{"id": i, "reply_to": p, "parent_hidden": h} for i, p, h in (("a", None, False), ("b", "a", False), ("c", "b", False), ("d", "z", True))]
_pg, _meta, _ref = _rl.thread_page(_rows, 2)
check("page 1", _ref is None and [r["id"] for r in _pg] == ["a", "b"] and _meta == {"total": 4, "returned": 2, "more": True, "next_after": "b"}, (_pg, _meta))
check("page 1 nothing elsewhere", [r["parent_elsewhere"] for r in _pg] == [False, False])
_pg, _meta, _ref = _rl.thread_page(_rows, 2, "b")
check("page 2 elsewhere vs hidden", [(r["id"], r["parent_elsewhere"], r["parent_hidden"]) for r in _pg] == [("c", True, False), ("d", False, True)] and _meta["more"] is False and _meta["next_after"] is None, _pg)
_pg, _meta, _ref = _rl.thread_page(_rows, 2, "d")
check("after the last row = empty, no more", _pg == [] and _meta["more"] is False and _meta["next_after"] is None)
for _bad in (0, 501, True, "2", None.__class__):
    check(f"limit {_bad!r} refused", _rl.thread_page(_rows, _bad)[2] is not None and _rl.thread_page(_rows, _bad)[2].code == "THREAD_PAGE_INVALID")
check("unknown after refused", _rl.thread_page(_rows, 2, "q")[2].code == "THREAD_PAGE_INVALID")
check("rows not changed in place", "parent_elsewhere" not in _rows[0])
check("tail none when complete", tcli._thread_tail({"more": False}, "bo", "x") is None)
check("tail none from an old tabd", tcli._thread_tail({"messages": []}, "bo", "x") is None)
check("tail line", tcli._thread_tail({"more": True, "next_after": "n1", "returned": 2, "total": 9}, "bo", "x") == "… more after this page (2 shown here, 9 in the thread). next: tabc thread --node bo --after n1 x")
check("thread line uses local time", tcli._thread_lines([_row], _kst) == ["  2026-11-16 15:36 KST ann → bo · s · " + "a" * tcli.ID_PREFIX_LEN])

print()
if FAILS:
    print(f"FAILED {len(FAILS)}: {FAILS}")
    sys.exit(1)
print("all passed")
