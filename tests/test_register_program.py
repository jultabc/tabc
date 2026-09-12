#!/usr/bin/env python3
"""register --program: route suppression unit tests.

Contract:
- --program suppresses routes structurally, even with an iTerm/tmux TTY.
  The option enforces the guarantee instead of relying on an operator to run from a daemon context (bob design 10-2).
- Without --program, _capture_route() captures the terminal route as before.
- Address form (--program host:port) remains route-less until Part 2 HTTP alarms are implemented.
Hermetic: stub call, _capture_route, _nodekey, and environment; no live database or network. Check the exit code."""

import contextlib
import io
import os
import sqlite3
import sys
import tempfile
import types

test_home = tempfile.mkdtemp(prefix="pgm_")
os.environ["TABC_HOME"] = test_home
os.environ["TABC_DB"] = os.path.join(test_home, "t.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tabus  # noqa: E402
from tabus import cli as tabc  # noqa: E402

fails = []


def check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        fails.append(name)


# Stub _capture_route to return a terminal route; --program must suppress it even here
# to prove suppression is structural.
tabc._user_email = lambda: None
tabc._host_id = lambda: "HID"
tabc._capture_route = lambda: ("iterm2", "SESSION-XYZ")
tabc._nodekey = lambda: types.SimpleNamespace(
    key_path=lambda n: f"/tmp/.k.{n}", public_key_b58=lambda p: "PUB"
)

captured = {}
captured_acting = {"node": None}


def fake_call(method, path, payload, node=None):
    captured.clear()
    captured.update(payload)
    captured_acting["node"] = node
    response = {"ok": True, "msg": f"registered {payload['node']}", "auto_enter_policy": "preserve-same-route"}
    if "auto_enter" in payload:
        response["auto_enter"] = payload["auto_enter"]
    return 200, response


tabc.call = fake_call


def A(**kw):
    return types.SimpleNamespace(**kw)


# 1. Ordinary registration without --program includes the available terminal adapter.
tabc.fn_register(A(node="tab1", kind="claude", program=None))
check("ordinary: includes the terminal route (adapter=iterm2)", captured.get("adapter") == "iterm2")
check("ordinary: includes host_id", captured.get("host_id") == "HID")
check("ordinary: signs with the registering node's own key", captured_acting["node"] == "tab1")

# 2. --program without an address excludes adapter despite an available terminal route.
tabc.fn_register(A(node="sample-program", kind="engine", program=""))
check("program: excludes adapter despite an available terminal", "adapter" not in captured)
check("program: also excludes host_id", "host_id" not in captured)
check(
    "program: preserves node and kind",
    captured.get("node") == "sample-program" and captured.get("kind") == "engine",
)
check("program: sends the send-only flag to the server", captured.get("program") is True)
check("program: signs with the registering node's own key", captured_acting["node"] == "sample-program")

# 3. Address-form --program remains route-less until Part 2 is implemented.
tabc.fn_register(A(node="other-program", kind="engine", program="host.local:9000"))
check("address-form program: currently excludes adapter (Part 2)", "adapter" not in captured)

# Server rejection must not be followed by receiving or route guidance that implies success.
tabc.call = lambda *args, **kwargs: (401, {"error": "signature did not verify"})
failure_output = io.StringIO()
try:
    with contextlib.redirect_stdout(failure_output):
        tabc.fn_register(A(node="rejected", kind="codex", program=None))
    check("registration failure exits 1", False)
except SystemExit as exc:
    check("registration failure exits 1", exc.code == 1)
check(
    "registration failure prints no receiving or route success guidance",
    "Mail arrives by pulling" not in failure_output.getvalue()
    and "no route" not in failure_output.getvalue(),
)

# Part 1.5: bus_register revokes an old terminal route when registering --program (real temporary database).
rcon = tabus.connect()
rcon.executescript(tabus.SCHEMA)
rcon.commit()

# Existing installed databases must also acquire the send-only column on restart.
# Testing only fresh SCHEMA creation would miss upgrade-only failures.
old_db = os.path.join(tempfile.mkdtemp(prefix="pgm_migrate_"), "old.db")
old_con = sqlite3.connect(old_db)
old_con.row_factory = sqlite3.Row
old_con.execute("CREATE TABLE nodes (node_id TEXT PRIMARY KEY)")
old_con.commit()
tabus.migrate(old_con)
old_cols = {r["name"] for r in old_con.execute("PRAGMA table_info(nodes)")}
old_default = old_con.execute(
    "SELECT dflt_value FROM pragma_table_info('nodes') WHERE name='is_program'"
).fetchone()
check("migration: adds is_program to an existing database", "is_program" in old_cols)
check(
    "migration: existing nodes default to receiving-enabled value 0",
    old_default is not None and old_default["dflt_value"] == "0",
)
old_con.close()

# Register sample-program with adapter, target, and host_id to create one active route.
tabus.bus_register(
    rcon, "sample-program", "engine", adapter="iterm2", target="TAB1", host_id="H1"
)
active = rcon.execute(
    "SELECT COUNT(*) c FROM tab_routes WHERE node_id='sample-program' AND revoked_at IS NULL"
).fetchone()["c"]
check("revoke: terminal registration creates an active route", active == 1)

# An existing agent cannot become a program or lose its active route.
_ok, msg = tabus.bus_register(rcon, "sample-program", "engine", program=True)
still = rcon.execute(
    "SELECT COUNT(*) c FROM tab_routes WHERE node_id='sample-program' AND revoked_at IS NULL"
).fetchone()["c"]
check("conversion: existing route remains active", not _ok and still == 1)
check("conversion: response explains the separate-name rule", "separate program name" in msg)
is_program = rcon.execute(
    "SELECT is_program FROM nodes WHERE node_id='sample-program'"
).fetchone()["is_program"]
check("conversion: preserves the receiving flag", is_program == 0)
tabus.bus_register(rcon, "event-bot", "engine", program=True)
check("program: existing program can register again",
      tabus.bus_register(rcon, "event-bot", "engine", program=True)[0])

# Program nodes are send-only; reject envelopes targeting them before writing any rows.
tabus.bus_register(rcon, "alice", "agent")
blocked_mid, blocked_reason = tabus.bus_send(
    rcon, "alice", ["event-bot"], "should block", "body"
)
check(
    "program: recipient selection is rejected before storage",
    blocked_mid is None and "send-only" in blocked_reason,
)
check(
    "program: rejected envelope creates zero envelope/delivery rows",
    rcon.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"] == 0
    and rcon.execute("SELECT COUNT(*) c FROM deliveries").fetchone()["c"] == 0,
)
event_mid, event_info = tabus.bus_send(
    rcon, "event-bot", ["alice"], "event", "ENTRY_SIGNAL"
)
check(
    "program: sending is allowed",
    event_mid is not None and event_info["recipients"] == ["alice"],
)

# Unread deliveries may remain from before the mode switch. A program cannot read them,
# so that history must not block subsequent event sending.
tabus.bus_register(rcon, "legacy_program", "engine")
legacy_mid, _ = tabus.bus_send(
    rcon, "alice", ["legacy_program"], "old incoming", "before program mode"
)
# Simulate historical stored state, not an allowed registration transition.
rcon.execute("UPDATE nodes SET is_program=1 WHERE node_id='legacy_program'")
rcon.commit()
legacy_event_mid, legacy_event_info = tabus.bus_send(
    rcon, "legacy_program", ["alice"], "event after switch", "ENTRY_SIGNAL"
)
check("program: unread messages from before switching do not block sending", legacy_mid is not None)
check(
    "program: event sending is allowed after switching",
    legacy_event_mid is not None and legacy_event_info["recipients"] == ["alice"],
)

# A previously route-less node reports zero revocations, not a false revocation claim.
_ok2, msg2 = tabus.bus_register(rcon, "ivan", "engine", program=True)
check("revoke: previously route-less node reports zero revocations", _ok2 and "reclaimed" not in msg2)

# Ordinary re-registration with program=False preserves the route (regression check).
tabus.bus_register(
    rcon, "tabx", "claude", adapter="iterm2", target="TAB2", host_id="H1"
)
tabus.bus_register(
    rcon, "tabx", "claude", adapter="iterm2", target="TAB2", host_id="H1"
)
tabx_active = rcon.execute(
    "SELECT COUNT(*) c FROM tab_routes WHERE node_id='tabx' AND revoked_at IS NULL"
).fetchone()["c"]
check("revoke: ordinary re-registration preserves the route", tabx_active == 1)

# Ordinary re-registration also clears the program flag for a consistent explicit mode switch.
tabus.bus_register(rcon, "sample-program", "engine")
program_after_attended = rcon.execute(
    "SELECT is_program FROM nodes WHERE node_id='sample-program'"
).fetchone()["is_program"]
check("ordinary re-registration clears the program flag", program_after_attended == 0)

print()
if fails:
    print(f"❌ {len(fails)} FAIL: {fails}")
    sys.exit(1)
print("✅ ALL PASS")
