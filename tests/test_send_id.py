#!/usr/bin/env python3
"""send --id: a caller-chosen message UUID makes a retry safe.

This runs the real HTTP client and daemon with an isolated state directory.
The daemon already treated a repeated id with an identical envelope as a resend
and a repeated id with a changed envelope as a conflict; the CLI could not reach
that because it always chose a fresh uuid4. These checks pin the contract a
retrying caller relies on:

- the same id and envelope stores one message and one delivery per recipient
- the response to a resend says so, and exits 0
- a changed envelope under the same id is refused and the stored body is kept
- a read-before-send refusal still comes first, so a resend is not always a success
- a program node, which skips the direct-message read gate, can always resend
- concurrent retries of one id store it once
"""

import contextlib
import io
import os
import re
import sqlite3
import sys
import tempfile
import threading
import types
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="send_id_")
DB_PATH = os.path.join(TMP, "tabc.db")
os.environ["TABC_DB"] = DB_PATH
os.environ["TABC_HOME"] = TMP
os.environ.pop("TABC_NODE", None)
sys.path.insert(0, ROOT)

import tabus  # noqa: E402,F401
from tabus import cli as tabc  # noqa: E402
from tabus import daemon as tabd  # noqa: E402

fails = []


def check(name, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {name}{(' — ' + detail) if detail else ''}")
    if not condition:
        fails.append(name)


def run(fn, args):
    output = io.StringIO()
    try:
        with contextlib.redirect_stdout(output):
            fn(args)
        return True, output.getvalue()
    except SystemExit as exc:
        return False, output.getvalue() + f"\nSystemExit({exc.code})"
    except OSError as exc:
        # A request that should not have been made, such as one to a closed port.
        return False, output.getvalue() + f"\n{exc!r}"


def args(**values):
    return types.SimpleNamespace(**values)


def send(sender, body, message_id, to=None, tac=None, subject="s"):
    values = dict(
        sender=sender,
        to=to or [],
        tac=tac,
        broadcast=False,
        subject=subject,
        body=body,
        body_file=None,
        priority="next",
    )
    if message_id is not None:
        values["id"] = message_id
    return run(tabc.fn_send, args(**values))


def scalar(sql, params=()):
    con = sqlite3.connect(DB_PATH)
    row = con.execute(sql, params).fetchone()
    con.close()
    return row[0] if row else None


def message_count(message_id):
    return scalar("SELECT COUNT(*) FROM messages WHERE id=?", (message_id,))


def delivery_count(message_id):
    return scalar("SELECT COUNT(*) FROM deliveries WHERE message_id=?", (message_id,))


class QuietServer(tabd.ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass


tabc._capture_route = lambda: (None, None)
tabc._user_email = lambda: None

# The parser accepts --id; a namespace without it keeps the old uuid4 behaviour.
parsed = tabc.build_parser().parse_args(
    ["send", "--sender", "a", "--to", "b", "--subject", "s", "--body", "b", "--id", "x"]
)
check("parser accepts --id", parsed.id == "x")
parsed = tabc.build_parser().parse_args(
    ["send", "--sender", "a", "--to", "b", "--subject", "s", "--body", "b"]
)
check("--id defaults to None", parsed.id is None)

tabd.init_extras()
server = QuietServer(("127.0.0.1", 0), tabd.BusHandler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
try:
    tabc.BASE = f"http://127.0.0.1:{server.server_port}"
    for node, program in (("alice", None), ("bob", None), ("carol", None), ("watch", "")):
        ok, out = run(tabc.fn_register, args(node=node, kind="generic", program=program))
        check(f"register {node}", ok, out.strip())

    # A malformed id is refused locally, before any request.
    ok, out = send("alice", "b", "not-a-uuid", to=["bob"])
    check(
        "non-UUID --id refused before HTTP",
        not ok and "--id must be a full message UUID" in out and "stored id=" not in out,
        out.strip(),
    )
    ok, out = send("alice", "b", "1234", to=["bob"])
    check("short prefix --id refused", not ok and "full message UUID" in out, out.strip())
    # With --tac the name lookup is a request of its own; the id check comes first.
    live_base = tabc.BASE
    tabc.BASE = "http://127.0.0.1:9"
    ok, out = send("alice", "b", "not-a-uuid", tac="any-tac")
    tabc.BASE = live_base
    check(
        "non-UUID --id with --tac refused before the tac lookup",
        not ok and "--id must be a full message UUID" in out and "Errno" not in out,
        out.strip(),
    )

    # Without --id the CLI still mints a fresh id each time.
    ok1, out1 = send("alice", "plain one", None, to=["bob"])
    ok2, out2 = send("alice", "plain two", None, to=["bob"])
    ids = re.findall(r"stored id=([0-9a-f-]+)", out1 + out2)
    check("no --id: two sends, two ids", ok1 and ok2 and len(set(ids)) == 2, str(ids))

    # Same id, same envelope: one message, one delivery, reported as a resend.
    U = str(uuid.uuid4())
    ok, out = send("alice", "event 1", U, to=["bob"])
    check("first send stores the given id", ok and f"stored id={U}" in out, out.strip())
    check("first send: one message", message_count(U) == 1)
    check("first send: one delivery", delivery_count(U) == 1)

    ok, out = send("alice", "event 1", U, to=["bob"])
    check(
        "resend: exit 0 and says it was not stored twice",
        ok and f"stored id={U}" in out and "not stored twice" in out,
        out.strip(),
    )
    check("resend: still one message", message_count(U) == 1)
    check("resend: still one delivery", delivery_count(U) == 1)

    # An id typed in upper case is the same id.
    ok, out = send("alice", "event 1", U.upper(), to=["bob"])
    check(
        "upper-case form is canonicalised to the same id",
        ok and f"stored id={U}" in out and "not stored twice" in out and message_count(U) == 1,
        out.strip(),
    )

    # A resend after the recipient read it leaves the delivery state alone.
    ok, out = run(tabc.fn_open, args(node="bob", id=U))
    check("bob opens the message", ok, out.strip())
    ok, out = run(tabc.fn_ack, args(node="bob", id=U, state="READ"))
    check("bob reads the message", ok, out.strip())
    ok, out = send("alice", "event 1", U, to=["bob"])
    check(
        "resend after READ keeps READ",
        ok
        and scalar("SELECT state FROM deliveries WHERE message_id=?", (U,)) == "READ"
        and delivery_count(U) == 1,
        out.strip(),
    )

    # Same id, changed envelope: refused, stored content kept.
    for label, body, to, subject in (
        ("body", "event 1 changed", ["bob"], "s"),
        ("subject", "event 1", ["bob"], "other subject"),
        ("recipients", "event 1", ["bob", "carol"], "s"),
    ):
        ok, out = send("alice", body, U, to=to, subject=subject)
        check(
            f"changed {label}: refused as a conflict",
            not ok and "conflict" in out and "SystemExit(1)" in out,
            out.strip(),
        )
    check(
        "conflicts leave the stored body and deliveries alone",
        scalar("SELECT body FROM messages WHERE id=?", (U,)) == "event 1"
        and delivery_count(U) == 1,
    )

    # The read-before-send gate runs before the duplicate check: a resend from a
    # conversational node can be refused even though the message is stored.
    V = str(uuid.uuid4())
    ok, out = send("alice", "event 2", V, to=["carol"])
    check("alice sends V to carol", ok, out.strip())
    run(tabc.fn_open, args(node="carol", id=V))
    ok, out = send("carol", "a reply", None, to=["alice"])
    check("carol writes to alice", ok, out.strip())
    ok, out = send("alice", "event 2", V, to=["carol"])
    check(
        "resend while alice has unread mail from carol is refused, not a success",
        not ok and "UNREAD_BLOCKED" in out and message_count(V) == 1,
        out.strip(),
    )
    reply_id = scalar(
        "SELECT id FROM messages WHERE sender_id='carol' ORDER BY rowid DESC LIMIT 1"
    )
    run(tabc.fn_open, args(node="alice", id=reply_id))
    ok, out = send("alice", "event 2", V, to=["carol"])
    check(
        "after alice opens it, the resend is recognised",
        ok and "not stored twice" in out and message_count(V) == 1,
        out.strip(),
    )

    # A program node is a one-way event source and skips the direct-message gate.
    W = str(uuid.uuid4())
    ok, out = send("watch", "alert 1", W, to=["alice"])
    check("program node sends W", ok and f"stored id={W}" in out, out.strip())
    send("alice", "unrelated", None, to=["bob"])
    ok, out = send("watch", "alert 1", W, to=["alice"])
    check(
        "program node resend is recognised",
        ok and "not stored twice" in out and message_count(W) == 1 and delivery_count(W) == 1,
        out.strip(),
    )

    # Tac sends: the envelope includes the member list at the time of sending.
    ok, out = run(
        tabc.fn_tac,
        args(action="create", tac="alerts", label="alerts", node="alice", by=None),
    )
    check("create tac", ok, out.strip())
    for member in ("alice", "bob"):
        ok, out = run(
            tabc.fn_tac,
            args(action="add", tac="alerts", member=member, node="alice", by=None),
        )
        check(f"add {member} to tac", ok, out.strip())
    T = str(uuid.uuid4())
    ok, out = send("alice", "tac event", T, tac="alerts")
    check("tac send with --id", ok and f"stored id={T}" in out, out.strip())
    ok, out = send("alice", "tac event", T, tac="alerts")
    check(
        "tac resend is recognised",
        ok and "not stored twice" in out and delivery_count(T) == 1,
        out.strip(),
    )
    run(tabc.fn_tac, args(action="add", tac="alerts", member="carol", node="alice", by=None))
    ok, out = send("alice", "tac event", T, tac="alerts")
    check(
        "tac resend after membership grew is a conflict, not a second message",
        not ok and "conflict" in out and delivery_count(T) == 1,
        out.strip(),
    )

    # The daemon compares resolved recipients, not the sending mode: the same id sent
    # by tac and then directly to the same members is taken as a resend. Callers
    # keep the mode fixed; this pins the current behaviour so a change is visible.
    run(tabc.fn_tac, args(action="rm", tac="alerts", member="carol", node="alice", by=None))
    ok, out = send("alice", "tac event", T, to=["bob"])
    check(
        "same recipients by --to after --tac: taken as a resend (mode not compared)",
        ok and "not stored twice" in out and delivery_count(T) == 1,
        out.strip(),
    )

    # Concurrent retries of one id store it once.
    X = str(uuid.uuid4())
    results = []
    lock = threading.Lock()

    # The HTTP call itself, not fn_send: redirect_stdout is process-wide and
    # would interleave the threads' output.
    payload = {
        "from": "watch",
        "to": ["bob"],
        "subject": "s",
        "body": "alert 2",
        "priority": "next",
        "message_id": X,
    }

    def attempt():
        code, r = tabc.call("POST", tabc.MAPPING["send"], dict(payload), node="watch")
        with lock:
            results.append((code == 200 and r.get("id") == X, f"{code} {r.get('info') or r.get('error')}"))

    workers = [threading.Thread(target=attempt) for _ in range(6)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=30)
    check(
        "concurrent retries: every attempt exits 0",
        len(results) == 6 and all(ok for ok, _ in results),
        " / ".join(o for _, o in results),
    )
    check(
        "concurrent retries: one message, one delivery",
        message_count(X) == 1 and delivery_count(X) == 1,
        f"messages={message_count(X)} deliveries={delivery_count(X)}",
    )
finally:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)

print(f"\n{'FAIL' if fails else 'ALL PASS'} — {len(fails)} failed")
sys.exit(1 if fails else 0)
