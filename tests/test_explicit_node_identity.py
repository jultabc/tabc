#!/usr/bin/env python3
"""Identity comes only from an explicit command node or TABC_NODE.

This runs the real HTTP client and daemon with an isolated state directory. The
legacy node file says bob while every inbox action explicitly targets alice. The
file must be ignored: a handler that forgets its acting node must fail locally,
never inherit bob and send a request under the wrong identity.
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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="explicit_node_")
DB_PATH = os.path.join(TMP, "tabc.db")
os.environ["TABC_DB"] = DB_PATH
os.environ["TABC_HOME"] = TMP
os.environ.pop("TABC_NODE", None)
sys.path.insert(0, ROOT)

import tabus  # noqa: E402
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
    except ValueError as exc:
        return False, output.getvalue() + f"\nValueError({exc})"


def args(**values):
    return types.SimpleNamespace(**values)


def send(sender, recipient, subject, body):
    ok, output = run(
        tabc.fn_send,
        args(
            sender=sender,
            to=[recipient],
            tac=None,
            broadcast=False,
            subject=subject,
            body=body,
            body_file=None,
            priority="next",
        ),
    )
    match = re.search(r"stored id=([0-9a-f-]+)", output)
    check(f"send {subject}", ok and match is not None, output.strip())
    return match.group(1) if match else "missing"


def send_to_tac(sender, tac, subject, body):
    ok, output = run(
        tabc.fn_send,
        args(
            sender=sender,
            to=[],
            tac=tac,
            broadcast=False,
            subject=subject,
            body=body,
            body_file=None,
            priority="next",
        ),
    )
    match = re.search(r"stored id=([0-9a-f-]+)", output)
    check(f"send {subject} to tac", ok and match is not None, output.strip())
    return match.group(1) if match else "missing"


def delivery_state(message_id):
    con = sqlite3.connect(DB_PATH)
    row = con.execute(
        "SELECT state FROM deliveries WHERE message_id=? AND recipient_id='alice'",
        (message_id,),
    ).fetchone()
    con.close()
    return row[0] if row else None


def is_removed(node):
    con = sqlite3.connect(DB_PATH)
    row = con.execute(
        "SELECT 1 FROM removed_nodes WHERE node_id=?", (node,)
    ).fetchone()
    con.close()
    return row is not None


def tac_creator(tac):
    con = sqlite3.connect(DB_PATH)
    row = con.execute(
        "SELECT created_by FROM tacs WHERE tac_id=?", (tac,)
    ).fetchone()
    con.close()
    return row[0] if row else None


def tac_id_by_name(name):
    con = sqlite3.connect(DB_PATH)
    row = con.execute("SELECT tac_id FROM tacs WHERE name=?", (name,)).fetchone()
    con.close()
    return row[0] if row else None


def removal_actor(node, mode):
    con = sqlite3.connect(DB_PATH)
    row = con.execute(
        "SELECT removed_by FROM removals WHERE node_id=? AND mode=? ORDER BY id DESC LIMIT 1",
        (node, mode),
    ).fetchone()
    con.close()
    return row[0] if row else None


def stored_message(message_id):
    con = sqlite3.connect(DB_PATH)
    row = con.execute(
        "SELECT sender_id, subject, body FROM messages WHERE id=?", (message_id,)
    ).fetchone()
    con.close()
    return tuple(row) if row else None


class QuietServer(tabd.ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass


legacy_node_file = os.path.join(TMP, "node")
with open(legacy_node_file, "w", encoding="utf-8") as saved:
    saved.write("bob")
tabc._capture_route = lambda: (None, None)
tabc._user_email = lambda: None

tabd.init_extras()
server = QuietServer(("127.0.0.1", 0), tabd.BusHandler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
try:
    tabc.BASE = f"http://127.0.0.1:{server.server_port}"

    check("legacy shared node file is ignored", tabc._my_node() == "")

    # Register is the bootstrap case: no environment identity exists, so alice
    # must self-sign with the key whose public half is in this request.
    alice_registered, alice_output = run(
        tabc.fn_register, args(node="alice", kind="codex", program=None)
    )
    bob_registered, bob_output = run(
        tabc.fn_register, args(node="bob", kind="claude", program=None)
    )
    check("register alice uses alice identity", alice_registered, alice_output.strip())
    check("register bob uses bob identity", bob_registered, bob_output.strip())
    with open(legacy_node_file, encoding="utf-8") as saved:
        check("register does not rewrite the legacy node file", saved.read() == "bob")

    no_identity_ok, no_identity_output = run(tabc.fn_who, args())
    check(
        "command without explicit node or TABC_NODE fails before HTTP",
        not no_identity_ok
        and "TABC_NODE" in no_identity_output
        and "SystemExit(2)" in no_identity_output
        and "[tabd]" not in no_identity_output,
        no_identity_output.strip(),
    )
    os.environ["TABC_NODE"] = "alice"
    env_identity_ok, env_identity_output = run(tabc.fn_who, args())
    os.environ.pop("TABC_NODE", None)
    check(
        "TABC_NODE supplies identity when a command has no node option",
        env_identity_ok and "alice" in env_identity_output,
        env_identity_output.strip(),
    )

    os.environ["TABC_NODE"] = "alice"
    empty_node_ok, empty_node_output = run(
        tabc.fn_mailbox, args(node="", limit=20)
    )
    os.environ.pop("TABC_NODE", None)
    check(
        "an explicitly empty node never falls through to TABC_NODE",
        not empty_node_ok and "SystemExit(2)" in empty_node_output,
        empty_node_output.strip(),
    )

    unsafe_node_ok, unsafe_node_output = run(
        tabc.fn_mailbox, args(node="../../etc/passwd", limit=20)
    )
    check(
        "an unsafe explicit node is a clean usage error before HTTP",
        not unsafe_node_ok
        and "SystemExit(2)" in unsafe_node_output
        and "[tabd]" not in unsafe_node_output,
        unsafe_node_output.strip(),
    )

    mailbox_id = send("bob", "alice", "mailbox explicit", "mailbox body")
    os.environ["TABC_NODE"] = "bob"
    mailbox_ok, mailbox_output = run(
        tabc.fn_mailbox, args(node="alice", limit=20)
    )
    os.environ.pop("TABC_NODE", None)
    check(
        "mailbox --node alice overrides a conflicting TABC_NODE=bob",
        mailbox_ok and "mailbox explicit" in mailbox_output,
        mailbox_output.strip(),
    )

    pull_ok, pull_output = run(
        tabc.fn_pull, args(node="alice", limit=20, mode="full")
    )
    check("pull --node alice succeeds", pull_ok, pull_output.strip())
    check("pull body is printed", "mailbox body" in pull_output, pull_output.strip())
    check(
        "pull arrival ack keeps alice identity",
        delivery_state(mailbox_id) == "INJECTED"
        and "could not record arrival" not in pull_output,
        f"state={delivery_state(mailbox_id)}",
    )

    ack_ok, ack_output = run(
        tabc.fn_ack,
        args(node="alice", id=mailbox_id, state="READ"),
    )
    check(
        "ack --node alice signs as alice",
        ack_ok and delivery_state(mailbox_id) == "READ",
        ack_output.strip(),
    )

    read_id = send("bob", "alice", "read explicit", "read body")
    read_ok, read_output = run(tabc.fn_read, args(node="alice", limit=20))
    check(
        "read --node alice keeps alice identity for every request",
        read_ok
        and "read body" in read_output
        and delivery_state(read_id) == "READ"
        and "0 still unread" in read_output,
        read_output.strip(),
    )

    open_id = send("bob", "alice", "open explicit", "open body")
    open_ok, open_output = run(
        tabc.fn_open, args(node="alice", id=open_id)
    )
    check(
        "open --node alice and its arrival ack both sign as alice",
        open_ok
        and "open body" in open_output
        and delivery_state(open_id) == "INJECTED",
        open_output.strip(),
    )

    snooze_ok, snooze_output = run(
        tabc.fn_snooze, args(node="alice", duration="1")
    )
    check(
        "snooze --node alice signs as alice",
        snooze_ok and "snoozed" in snooze_output,
        snooze_output.strip(),
    )

    tac_create_ok, tac_create_output = run(
        tabc.fn_tac,
        args(
            action="create",
            tac="identity",
            label="Identity scope",
            node="alice",
            by=None,
        ),
    )
    check(
        "tac mutation --node alice signs as alice",
        tac_create_ok and "created" in tac_create_output,
        tac_create_output.strip(),
    )
    identity_tac = tac_id_by_name("identity")
    check(
        "fresh daemon creates a canonical UUID tac",
        bool(identity_tac and re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            identity_tac,
        )),
        str(identity_tac),
    )
    os.environ["TABC_NODE"] = "alice"
    tac_env_create_ok, tac_env_create_output = run(
        tabc.fn_tac,
        args(
            action="create",
            tac="env-identity",
            label="Environment identity",
            node=None,
            by=None,
        ),
    )
    os.environ.pop("TABC_NODE", None)
    env_identity_tac = tac_id_by_name("env-identity")
    check(
        "tac mutation records TABC_NODE as the audit actor",
        tac_env_create_ok and tac_creator(env_identity_tac) == "alice",
        f"created_by={tac_creator(env_identity_tac)} output={tac_env_create_output.strip()}",
    )
    tac_list_ok, tac_list_output = run(
        tabc.fn_tac,
        args(action="ls", node="alice"),
    )
    check(
        "tac ls --node alice signs as alice",
        tac_list_ok and "identity" in tac_list_output,
        tac_list_output.strip(),
    )
    tac_add_ok, tac_add_output = run(
        tabc.fn_tac,
        args(
            action="add",
            tac=identity_tac,
            member="alice",
            node="alice",
            by=None,
        ),
    )
    check(
        "tac add keeps alice identity",
        tac_add_ok and identity_tac in tac_add_output and "alice" in tac_add_output,
        tac_add_output.strip(),
    )
    tac_add_bob_ok, tac_add_bob_output = run(
        tabc.fn_tac,
        args(
            action="add",
            tac=identity_tac,
            member="bob",
            node="alice",
            by=None,
        ),
    )
    check(
        "tac add bob keeps alice identity",
        tac_add_bob_ok and "bob" in tac_add_bob_output,
        tac_add_bob_output.strip(),
    )
    tac_message_id = send_to_tac("bob", identity_tac, "observe only", "tac body")
    check(
        "tac delivery starts unread",
        delivery_state(tac_message_id) == "ACCEPTED",
        f"state={delivery_state(tac_message_id)}",
    )
    os.environ["TABC_NODE"] = "alice"
    tac_observe_ok, tac_observe_output = run(
        tabc.fn_tac,
        args(action="show", tac=identity_tac, limit=20, node=None),
    )
    os.environ.pop("TABC_NODE", None)
    check(
        "tac show without --node is observation-only",
        tac_observe_ok
        and "tac body" in tac_observe_output
        and "caught up" not in tac_observe_output
        and delivery_state(tac_message_id) == "ACCEPTED",
        f"state={delivery_state(tac_message_id)} output={tac_observe_output.strip()}",
    )
    tac_show_ok, tac_show_output = run(
        tabc.fn_tac,
        args(action="show", tac=identity_tac, limit=20, node="alice"),
    )
    check(
        "tac show --node alice signs as alice",
        tac_show_ok
        and "[tac identity]" in tac_show_output
        and "caught up" in tac_show_output
        and delivery_state(tac_message_id) == "INJECTED",
        tac_show_output.strip(),
    )

    dm_args = tabc.build_parser().parse_args(
        [
            "send",
            "--sender",
            "bob",
            "--to",
            "alice",
            "--subject",
            "dm alias",
            "--body",
            "same as send",
        ]
    )
    dm_ok, dm_output = run(tabc.COMMANDS[dm_args.cmd][0], dm_args)
    dm_match = re.search(r"stored id=([0-9a-f-]+)", dm_output)
    dm_id = dm_match.group(1) if dm_match else "missing"
    check(
        "send works through the real client and daemon",
        dm_ok
        and stored_message(dm_id) == ("bob", "dm alias", "same as send")
        and delivery_state(dm_id) == "ACCEPTED",
        dm_output.strip(),
    )

    # rm/restore use --node for the target, unlike mailbox/tac commands where it
    # names the acting node. They must therefore require TABC_NODE separately.
    rm_without_actor_ok, rm_without_actor_output = run(
        tabc.fn_rm,
        args(node="bob", purge=False, yes=True, by=None),
    )
    check(
        "rm target is not reused as the acting identity",
        not rm_without_actor_ok
        and "TABC_NODE" in rm_without_actor_output
        and "SystemExit(2)" in rm_without_actor_output
        and not is_removed("bob"),
        rm_without_actor_output.strip(),
    )

    os.environ["TABC_NODE"] = "alice"
    rm_ok, rm_output = run(
        tabc.fn_rm,
        args(node="bob", purge=False, yes=True, by=None),
    )
    os.environ.pop("TABC_NODE", None)
    check(
        "rm uses TABC_NODE as actor and --node as target",
        rm_ok
        and is_removed("bob")
        and not is_removed("alice")
        and removal_actor("bob", "soft") == "alice",
        rm_output.strip(),
    )

    restore_without_actor_ok, restore_without_actor_output = run(
        tabc.fn_restore,
        args(node="bob", by=None),
    )
    check(
        "restore target is not reused as the acting identity",
        not restore_without_actor_ok
        and "TABC_NODE" in restore_without_actor_output
        and "SystemExit(2)" in restore_without_actor_output
        and is_removed("bob"),
        restore_without_actor_output.strip(),
    )

    os.environ["TABC_NODE"] = "alice"
    restore_ok, restore_output = run(
        tabc.fn_restore,
        args(node="bob", by=None),
    )
    os.environ.pop("TABC_NODE", None)
    check(
        "restore uses TABC_NODE as actor and --node as target",
        restore_ok
        and not is_removed("bob")
        and not is_removed("alice")
        and removal_actor("bob", "restore") == "alice",
        restore_output.strip(),
    )
finally:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)

print(f"\n{'FAIL' if fails else 'ALL PASS'} — {len(fails)} failed")
sys.exit(1 if fails else 0)
