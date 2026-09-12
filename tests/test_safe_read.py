#!/usr/bin/env python3
"""Safe read (`tabc read`) — mark READ only what was shown, report remaining honestly.

The bug this guards against: list the unread, then blanket-ack every id from that
snapshot. Anything not actually shown (arrived mid-run, or beyond --limit) would be
marked read without its body ever printed. This pins the safe shape: with three
messages and --limit=2, exactly the two shown reach READ, the third stays unread,
and the closing count says '1 still unread' — never a false 'inbox clear'. Temp DB,
own HTTP server, judged by exit code.
"""

import contextlib
import io
import os
import sqlite3
import sys
import tempfile
import threading
import types

TMP = tempfile.mkdtemp(prefix="safe_read_")
DB_PATH = os.path.join(TMP, "beacon.db")
os.environ["TABC_DB"] = DB_PATH
os.environ["TABC_HOME"] = TMP  # keys + state isolated to the temp dir, off real ~/.tabc
os.environ["TABC_NODE"] = "alice"  # fallback for commands without an acting-node option
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tabus  # noqa: E402
from tabus import daemon as tabd  # noqa: E402
from tabus import cli as tabc  # noqa: E402
from tabus import nodekey  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{(' — ' + detail) if detail else ''}")
    if not cond:
        fails.append(name)


class QuietServer(tabd.ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass


def _counts(node):
    c = sqlite3.connect(DB_PATH)
    read = c.execute(
        "SELECT count(*) FROM deliveries WHERE recipient_id=? AND state='READ'", (node,)
    ).fetchone()[0]
    unread = c.execute(
        "SELECT count(*) FROM deliveries WHERE recipient_id=? AND state IN "
        "('ACCEPTED','CLAIMED','INJECTED')",
        (node,),
    ).fetchone()[0]
    c.close()
    return read, unread


tabd.init_extras()
# alice acts through the client, so the daemon must hold her pubkey — the client
# signs each request with the matching key. bob only ever appears as a stored sender.
alice_pub = nodekey.public_key_b58(nodekey.key_path("alice"))
con = tabus.connect()
tabus.bus_register(con, "alice", "generic", pubkey=alice_pub)
tabus.bus_register(con, "bob", "generic")
# bob → alice, three messages
for i in range(3):
    tabus.bus_send(con, "bob", ["alice"], f"subject {i}", f"body number {i}")
con.commit()
con.close()

server = QuietServer(("127.0.0.1", 0), tabd.BusHandler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
try:
    # 🔴 Point the client at the test server (call() reads these module globals).
    tabc.BASE = f"http://127.0.0.1:{server.server_port}"

    # read with a limit smaller than the inbox: only two are shown
    args = types.SimpleNamespace(node="alice", limit=2)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        tabc.fn_read(args)
    out = buf.getvalue()

    check("both shown bodies printed", "body number 0" in out and "body number 1" in out)
    check("🔴 the unshown third body was NOT printed", "body number 2" not in out)
    check("🔴 honest close reports 1 still unread (not a false clear)", "read 2 · 1 still unread" in out, out.strip().splitlines()[-2] if out.strip() else "")

    read, unread = _counts("alice")
    check("exactly 2 marked READ (only what was shown)", read == 2, f"read={read}")
    check("🔴 the third stays unread (never blanket-acked)", unread == 1, f"unread={unread}")

    # a second read clears the remaining one and reports zero honestly
    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        tabc.fn_read(types.SimpleNamespace(node="alice", limit=20))
    out2 = buf2.getvalue()
    check("second read shows the third body", "body number 2" in out2)
    check("second read reports 0 still unread", "read 1 · 0 still unread" in out2)

    read2, unread2 = _counts("alice")
    check("all three now READ", read2 == 3 and unread2 == 0, f"read={read2} unread={unread2}")

    # empty inbox says so, marks nothing
    buf3 = io.StringIO()
    with contextlib.redirect_stdout(buf3):
        tabc.fn_read(types.SimpleNamespace(node="alice", limit=20))
    check("empty inbox reports 'no unread'", "no unread" in buf3.getvalue())

    # 🔴 A FAILED closing query must not report zero — the exact false 'inbox clear'
    #    this command exists to prevent. call() returns (500, {"error":...}) — a dict
    #    with no "unread" key — so a naive len(...get("unread", [])) would read 0.
    #    Fail only the CLOSING mailbox query: succeed the first GET, fail after.
    con2 = tabus.connect()
    tabus.bus_send(con2, "bob", ["alice"], "late", "a late one")
    con2.commit()
    con2.close()
    real_call = tabc.call
    seen = {"mailbox_gets": 0}

    def flaky_call(method, path, payload=None, node=None):
        if method == "GET" and path.startswith("/mailbox"):
            seen["mailbox_gets"] += 1
            if seen["mailbox_gets"] >= 2:  # the closing re-query
                return 500, {"error": "boom"}
        return real_call(method, path, payload, node=node)

    tabc.call = flaky_call
    try:
        buf4 = io.StringIO()
        with contextlib.redirect_stdout(buf4):
            tabc.fn_read(types.SimpleNamespace(node="alice", limit=20))
        out4 = buf4.getvalue()
    finally:
        tabc.call = real_call
    check("🔴 failed closing query does NOT report '0 still unread'", "0 still unread" not in out4)
    check("🔴 failed closing query reports honest 'could not check'", "could not check" in out4)
    # the message was still read (body shown, marked) before the count failed
    check("the late message was still read despite the failed count", "a late one" in out4)
finally:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)

print(f"\n{'FAIL' if fails else 'ALL PASS'} — {len(fails)} failed")
sys.exit(1 if fails else 0)
