#!/usr/bin/env python3
"""Per-route automatic Enter is opt-in and defaults to off."""

import contextlib
import io
import os
import sqlite3
import sys
import tempfile
import types


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="doorbell_auto_enter_test_")
os.environ["TABC_DB"] = os.path.join(TMP, "tabus.db")
os.environ["DOORBELL_LEDGER"] = os.path.join(TMP, "doorbell_ring.db")
sys.path.insert(0, ROOT)

import tabus  # noqa: E402
from tabus import cli, doorbell  # noqa: E402


fails = []


def check(name, condition):
    print(f"  {'PASS' if condition else 'FAIL'}  {name}")
    if not condition:
        fails.append(name)


con = tabus.connect()
con.executescript(tabus.SCHEMA)
tabus.migrate(con)


def register(node, auto_enter=None):
    return tabus.bus_register(
        con,
        node,
        "codex",
        adapter="iterm2",
        target=f"w0:{node}",
        host_id="host-a",
        auto_enter=auto_enter,
    )


def stored_auto_enter(node):
    return con.execute(
        "SELECT auto_enter FROM tab_routes WHERE node_id=?", (node,)
    ).fetchone()["auto_enter"]


# Missing and explicit off are both off. Explicit on affects only that route.
ok_missing, _ = register("missing")
ok_off, _ = register("off", False)
ok_on, _ = register("on", True)
check("registration without a setting defaults to off", ok_missing and stored_auto_enter("missing") == 0)
check("explicit off is stored as off", ok_off and stored_auto_enter("off") == 0)
check("explicit on is stored only on that route", ok_on and stored_auto_enter("on") == 1)

# Omission preserves the same active route, not a new or revoked destination.
ok_reset, _ = register("on")
check("re-registering without a setting preserves on", ok_reset and stored_auto_enter("on") == 1)
ok_detached, _ = tabus.bus_register(con, "on", "codex")
check("route-less omission preserves on", ok_detached and stored_auto_enter("on") == 1)
ok_moved, _ = tabus.bus_register(con, "on", "codex", adapter="iterm2", target="new", host_id="host-a")
check("moving to a new route defaults off", ok_moved and stored_auto_enter("on") == 0)
register("revoked", True)
con.execute("UPDATE tab_routes SET revoked_at='old' WHERE node_id='revoked'")
con.commit()
ok_revoked, _ = register("revoked")
check("reclaiming revoked route defaults off", ok_revoked and stored_auto_enter("revoked") == 0)

register("detached", True)
detached_ok, detached_msg = tabus.bus_register(
    con, "detached", "codex", auto_enter=False
)
check(
    "route-less re-registration also turns the existing route off",
    detached_ok and stored_auto_enter("detached") == 0,
)
check(
    "route-less reset reports the changed automatic Enter setting",
    "auto_enter reset to off" in detached_msg,
)

register("program-switch", True)
before_program_attempt = list(con.iterdump())
program_ok, program_msg = tabus.bus_register(
    con, "program-switch", "engine", program=True, auto_enter=False
)
check(
    "an existing agent cannot be converted to a program",
    not program_ok and "cannot become a program" in program_msg,
)
check(
    "refused program conversion preserves all node and route state including Enter",
    list(con.iterdump()) == before_program_attempt
    and stored_auto_enter("program-switch") == 1,
)

# Taking an occupied route and enabling submission are two separate explicit
# choices. When both are present, both effects must survive one transaction.
register("take-owner", False)
take_ok, take_msg = tabus.bus_register(
    con,
    "take-new",
    "codex",
    adapter="iterm2",
    target="w0:take-owner",
    host_id="host-a",
    take_route=True,
    auto_enter=True,
)
check(
    "explicit route takeover may enable automatic Enter in the same transaction",
    take_ok
    and stored_auto_enter("take-new") == 1
    and "take-owner" in take_msg
    and con.execute(
        "SELECT revoked_at FROM tab_routes WHERE node_id='take-owner'"
    ).fetchone()["revoked_at"]
    is not None,
)

# Automatic submission cannot be enabled without a complete destination. Refuse
# before creating the node so a 409 cannot look like partial registration.
no_route_ok, no_route_msg = tabus.bus_register(
    con, "no-route", "codex", auto_enter=True
)
check(
    "core refuses automatic Enter without a complete route",
    no_route_ok is False and "complete terminal route" in no_route_msg,
)
check(
    "refused route-less opt-in creates no node",
    con.execute("SELECT 1 FROM nodes WHERE node_id='no-route'").fetchone() is None,
)

# The server-facing core rejects non-booleans instead of treating a truthy string as on.
ok_bad, bad_msg = register("bad", "on")
check("a non-boolean setting is rejected", ok_bad is False and "boolean" in bad_msg)
check(
    "a rejected setting creates no route",
    con.execute("SELECT 1 FROM tab_routes WHERE node_id='bad'").fetchone() is None,
)

# The command accepts only on/off and keeps omission off.
parser = cli.build_parser()
missing_args = parser.parse_args(["register", "--node", "alice"])
on_args = parser.parse_args(["register", "--node", "alice", "--auto-enter", "on"])
off_args = parser.parse_args(["register", "--node", "alice", "--auto-enter", "off"])
name_on_args = parser.parse_args(["register", "--node", "alice", "--auto-enter", "on"])
take_on_args = parser.parse_args(
    [
        "register",
        "--node",
        "alice",
        "--take-route",
        "--auto-enter",
        "on",
    ]
)
check("CLI omission parses as unspecified", missing_args.auto_enter is None)
check("CLI accepts on", on_args.auto_enter == "on")
check("CLI accepts off", off_args.auto_enter == "off")
check("register minimal accepts the same on setting", name_on_args.auto_enter == "on")
check(
    "CLI keeps route takeover and automatic Enter as two explicit flags",
    take_on_args.take_route is True and take_on_args.auto_enter == "on",
)
try:
    parser.parse_args(["register", "--node", "alice", "--auto-enter", "yes"])
except SystemExit as exc:
    invalid_rejected = exc.code != 0
else:
    invalid_rejected = False
check("CLI rejects values other than on/off", invalid_rejected)

# Both bootstrap commands stop locally when ON has no captured route. The HTTP
# call must never run, even if a future refactor moves one of these guards.
original_capture_route = cli._capture_route
original_nodekey = cli._nodekey
original_user_email = cli._user_email
original_host_id = cli._host_id
original_call = cli.call
http_called = []


class FakeNodeKey:
    @staticmethod
    def key_path(node):
        return os.path.join(TMP, f"key-{node}")

    @staticmethod
    def public_key_b58(_path):
        return "test-public-key"


def forbidden_call(*_args, **_kwargs):
    http_called.append(True)
    raise AssertionError("route-less auto-enter reached HTTP")


try:
    cli._capture_route = lambda: (None, None)
    cli._nodekey = lambda: FakeNodeKey
    cli._user_email = lambda: None
    cli.call = forbidden_call
    for command, fn, args in (
        (
            "register",
            cli.fn_register,
            types.SimpleNamespace(
                node="cli-register-no-route",
                kind="codex",
                take_route=False,
                program=None,
                auto_enter="on",
            ),
        ),
        (
            "register minimal",
            cli.fn_register,
            types.SimpleNamespace(
                node="cli-name-no-route",
                kind="codex",
                take_route=False,
                auto_enter="on",
            ),
        ),
        (
            "program",
            cli.fn_register,
            types.SimpleNamespace(
                node="cli-program-no-route",
                kind="engine",
                take_route=False,
                program="",
                auto_enter="on",
            ),
        ),
    ):
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                fn(args)
        except SystemExit as exc:
            refused = exc.code == 2
        except AssertionError:
            refused = False
        else:
            refused = False
        check(f"{command} refuses automatic Enter without a route", refused)
finally:
    cli._capture_route = original_capture_route
    cli._nodekey = original_nodekey
    cli._user_email = original_user_email
    cli._host_id = original_host_id
    cli.call = original_call

check("route-less CLI refusal makes no HTTP request", not http_called)

# A new CLI must not silently accept an older daemon that ignores auto_enter.
# Real parser namespaces always carry the option, including its default OFF.
captured_payloads = []


def confirming_call(_method, _path, payload, node=None):
    captured_payloads.append(dict(payload))
    return 200, {
        "ok": True,
        "msg": f"registered {node}, route={payload.get('adapter')}",
        "auto_enter": payload.get("auto_enter"),
    }


def old_daemon_call(_method, _path, _payload, node=None):
    return 200, {"ok": True, "msg": f"registered {node}, route=tmux"}


try:
    cli._capture_route = lambda: ("tmux", "/tmp/test.sock\t%7")
    cli._nodekey = lambda: FakeNodeKey
    cli._user_email = lambda: None
    cli._host_id = lambda: "test-host"

    cli.call = confirming_call
    with contextlib.redirect_stdout(io.StringIO()):
        cli.fn_register(
            types.SimpleNamespace(
                node="register-bool-on",
                kind="codex",
                take_route=False,
                program=None,
                auto_enter="on",
            )
        )
        cli.fn_register(
            types.SimpleNamespace(
                node="register-bool-off",
                kind="codex",
                take_route=False,
                program=None,
                auto_enter="off",
            )
        )
    check(
        "register sends literal booleans for on and off",
        [p.get("auto_enter") for p in captured_payloads] == [True, False]
        and all(type(p.get("auto_enter")) is bool for p in captured_payloads),
    )

    # Route-less OFF is still a state change request. Sending and confirming it
    # prevents an older daemon from silently keeping a previously enabled route.
    captured_payloads.clear()
    cli._capture_route = lambda: (None, None)
    with contextlib.redirect_stdout(io.StringIO()):
        cli.fn_register(
            types.SimpleNamespace(
                node="register-route-less-off",
                kind="codex",
                take_route=False,
                program=None,
                auto_enter="off",
            )
        )
    check(
        "route-less register sends and confirms literal off",
        len(captured_payloads) == 1
        and captured_payloads[0].get("auto_enter") is False,
    )

    cli.call = old_daemon_call
    output = io.StringIO()
    try:
        with contextlib.redirect_stdout(output):
            cli.fn_register(
                types.SimpleNamespace(
                    node="register-route-less-old-daemon",
                    kind="codex",
                    take_route=False,
                    program=None,
                    auto_enter="off",
                )
            )
    except SystemExit as exc:
        route_less_old_refused = (
            exc.code == 1
            and "auto_enter=off" in output.getvalue()
            and "registered register-route-less-old-daemon" not in output.getvalue()
        )
    else:
        route_less_old_refused = False
    check(
        "route-less register refuses a daemon that does not confirm the setting",
        route_less_old_refused,
    )

    cli._capture_route = lambda: ("tmux", "/tmp/test.sock\\t%7")
    for command, fn, args, expected in (
        (
            "register",
            cli.fn_register,
            types.SimpleNamespace(
                node="register-old-daemon",
                kind="codex",
                take_route=False,
                program=None,
                auto_enter="off",
            ),
            "auto_enter=off",
        ),
        (
            "register minimal",
            cli.fn_register,
            types.SimpleNamespace(
                node="name-old-daemon",
                kind="codex",
                take_route=False,
                auto_enter="on",
            ),
            "auto_enter=on",
        ),
        (
            "register without a parser-provided option",
            cli.fn_register,
            types.SimpleNamespace(
                node="register-programmatic-old-daemon",
                kind="codex",
                take_route=False,
                program=None,
            ),
            "did not confirm setting preservation",
        ),
    ):
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output):
                fn(args)
        except SystemExit as exc:
            refused = (
                exc.code == 1
                and expected in output.getvalue()
                and f"registered {args.node}, route=tmux" not in output.getvalue()
            )
        else:
            refused = False
        check(f"{command} refuses a daemon that does not confirm the setting", refused)

    # The old daemon includes caller-controlled kind text in its prose response.
    # That text must not impersonate the dedicated boolean confirmation field.
    deceptive = types.SimpleNamespace(
        node="register-deceptive-kind",
        kind="auto_enter=off",
        take_route=False,
        program=None,
        auto_enter="off",
    )
    output = io.StringIO()
    try:
        with contextlib.redirect_stdout(output):
            cli.fn_register(deceptive)
    except SystemExit as exc:
        deceptive_refused = exc.code == 1
    else:
        deceptive_refused = False
    check(
        "caller-controlled kind cannot forge the daemon confirmation",
        deceptive_refused,
    )
finally:
    cli._capture_route = original_capture_route
    cli._nodekey = original_nodekey
    cli._user_email = original_user_email
    cli._host_id = original_host_id
    cli.call = original_call

# A database created by an older release receives the column with off for every
# existing route. This is the deployment path, not only the fresh-schema path.
old_db = sqlite3.connect(os.path.join(TMP, "old.db"))
old_db.row_factory = sqlite3.Row
old_db.execute(
    "CREATE TABLE tab_routes (node_id TEXT PRIMARY KEY, adapter TEXT NOT NULL, "
    "target TEXT NOT NULL, host_id TEXT, registered_at TEXT NOT NULL, "
    "last_seen_at TEXT NOT NULL, revoked_at TEXT)"
)
old_db.execute(
    "INSERT INTO tab_routes VALUES ('legacy','tmux','%1','host-a','t0','t0',NULL)"
)
old_db.commit()
tabus.migrate(old_db)
legacy = old_db.execute(
    "SELECT auto_enter FROM tab_routes WHERE node_id='legacy'"
).fetchone()
check("migration turns an existing route off", legacy["auto_enter"] == 0)
fresh_route_columns = [
    row["name"] for row in con.execute("PRAGMA table_info(tab_routes)")
]
migrated_route_columns = [
    row["name"] for row in old_db.execute("PRAGMA table_info(tab_routes)")
]
check(
    "fresh and migrated route tables keep the same column order",
    fresh_route_columns == migrated_route_columns,
)
old_db.close()

# The doorbell may restart before tabd performs the migration. It must still read
# an older route as off instead of failing or enabling Enter.
unmigrated_path = os.path.join(TMP, "unmigrated.db")
unmigrated = sqlite3.connect(unmigrated_path)
unmigrated.execute(
    "CREATE TABLE tab_routes (node_id TEXT PRIMARY KEY, adapter TEXT NOT NULL, "
    "target TEXT NOT NULL, host_id TEXT, registered_at TEXT NOT NULL, "
    "last_seen_at TEXT NOT NULL, revoked_at TEXT)"
)
unmigrated.execute(
    "INSERT INTO tab_routes VALUES ('legacy','tmux','%1','host-a','t0','t0',NULL)"
)
unmigrated.commit()
unmigrated.close()
original_doorbell_db = doorbell.TABC_DB
try:
    doorbell.TABC_DB = unmigrated_path
    check(
        "doorbell reads an unmigrated route as off",
        doorbell.route_for("legacy") == ("tmux", "%1", False),
    )
finally:
    doorbell.TABC_DB = original_doorbell_db

check(
    "doorbell reads an enabled route from the migrated database",
    doorbell.route_for("take-new") == ("iterm2", "w0:take-owner", True),
)

# The same per-route switch reaches both terminal adapters.
original_unread = doorbell.unread_senders
original_route = doorbell.route_for
original_iterm = doorbell.send_to_iterm_session
original_tmux = doorbell.send_to_tmux_pane
calls = []


def capture_iterm(target, message, enter=False):
    calls.append(("iterm2", target, enter))
    return doorbell.DELIVER_SUCCESS, 1


def capture_tmux(target, message, enter=False):
    calls.append(("tmux", target, enter))
    return doorbell.DELIVER_SUCCESS, 1


try:
    doorbell.unread_senders = lambda _node: [{"sender": "alice", "count": 1}]
    doorbell.send_to_iterm_session = capture_iterm
    doorbell.send_to_tmux_pane = capture_tmux

    routes = {
        "iterm-off": ("iterm2", "w0:OFF", False),
        "iterm-on": ("iterm2", "w0:ON", True),
        "tmux-off": ("tmux", "/tmp/tmux.sock\t%1", False),
        "tmux-on": ("tmux", "/tmp/tmux.sock\t%2", True),
    }
    doorbell.route_for = lambda node: routes[node]
    for node in routes:
        doorbell.deliver_doorbell(node, 1)
finally:
    doorbell.unread_senders = original_unread
    doorbell.route_for = original_route
    doorbell.send_to_iterm_session = original_iterm
    doorbell.send_to_tmux_pane = original_tmux

check("iTerm off does not request Enter", ("iterm2", "w0:OFF", False) in calls)
check("iTerm on requests Enter", ("iterm2", "w0:ON", True) in calls)
check("tmux off does not request Enter", ("tmux", "/tmp/tmux.sock\t%1", False) in calls)
check("tmux on requests Enter", ("tmux", "/tmp/tmux.sock\t%2", True) in calls)

# The low-level adapters are also off by default, so a future caller that omits
# the setting cannot accidentally restore the old behaviour.
original_run = doorbell.subprocess.run


class RunResult:
    returncode = 0
    stdout = "1"
    stderr = ""


run_calls = []


def capture_run(args, **kwargs):
    run_calls.append(args)
    return RunResult()


try:
    doorbell.subprocess.run = capture_run
    doorbell.send_to_iterm_session("w0:GUID", "bell")
    iterm_off_script = run_calls[-1][-1]
    doorbell.send_to_iterm_session("w0:GUID", "bell", enter=True)
    iterm_on_script = run_calls[-1][-1]

    run_calls.clear()
    doorbell.send_to_tmux_pane("/tmp/tmux.sock\t%1", "bell")
    tmux_off_calls = list(run_calls)
    run_calls.clear()
    doorbell.send_to_tmux_pane("/tmp/tmux.sock\t%1", "bell", enter=True)
    tmux_on_calls = list(run_calls)
finally:
    doorbell.subprocess.run = original_run

check(
    "iTerm adapter default types once without an implicit or explicit Enter",
    'write text "bell" newline NO' in iterm_off_script
    and iterm_off_script.count("write text") == 1
    and "ASCII character 13" not in iterm_off_script,
)
check(
    "iTerm adapter on types without an implicit newline, then presses Enter once",
    'write text "bell" newline NO' in iterm_on_script
    and iterm_on_script.count("write text") == 2
    and iterm_on_script.count("ASCII character 13") == 1,
)
check(
    "tmux adapter default makes one literal text call with no Enter",
    len(tmux_off_calls) == 1
    and "-l" in tmux_off_calls[0]
    and "Enter" not in tmux_off_calls[0],
)
check(
    "tmux adapter on adds exactly one separate Enter call",
    len(tmux_on_calls) == 2
    and "Enter" not in tmux_on_calls[0]
    and tmux_on_calls[1][-1] == "Enter",
)

con.close()
print(f"\n{'FAIL' if fails else 'ALL PASS'} — {len(fails)} failed")
sys.exit(1 if fails else 0)
