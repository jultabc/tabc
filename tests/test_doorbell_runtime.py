#!/usr/bin/env python3
"""Runtime regressions for sender lookup and the delivery return contract."""

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
original_ledger_env = os.environ.get("DOORBELL_LEDGER")
os.environ["DOORBELL_LEDGER"] = os.path.join(tempfile.mkdtemp(), "ring.db")
sys.path.insert(0, ROOT)

from tabus import doorbell  # noqa: E402

fails = []


def check(name, condition):
    print(f"  {'PASS' if condition else 'FAIL'}  {name}")
    if not condition:
        fails.append(name)


class FakeConnection:
    row_factory = None

    def close(self):
        pass


original_connect = doorbell.sqlite3.connect
original_bus_unread = doorbell.bus.bus_unread_senders
original_unread = doorbell.unread_senders
original_route = doorbell.route_for
original_send = doorbell.send_to_iterm_session

try:
    sender_calls = []

    def fake_unread_senders(con, node, include_tac=False):
        assert include_tac is True
        sender_calls.append((con, node))
        return [{"sender": "alice", "count": 1}]

    doorbell.sqlite3.connect = lambda *args, **kwargs: FakeConnection()
    doorbell.bus.bus_unread_senders = fake_unread_senders
    check(
        "sender lookup calls the imported bus module",
        doorbell.unread_senders("bob") == [{"sender": "alice", "count": 1}]
        and len(sender_calls) == 1
        and isinstance(sender_calls[0][0], FakeConnection)
        and sender_calls[0][1] == "bob",
    )

    captured = {}

    def capture(target, message, enter=True):
        captured["target"] = target
        captured["message"] = message
        return doorbell.DELIVER_SUCCESS, 1

    doorbell.unread_senders = lambda node: None
    doorbell.route_for = lambda node: ("iterm2", "w0:GUID")
    doorbell.send_to_iterm_session = capture
    result = doorbell.deliver_doorbell("bob", 2)
    check(
        "lookup failure keeps the three-field delivery contract",
        result == (doorbell.DELIVER_UNKNOWN, "metadata", -1),
    )
    check(
        "lookup failure never injects a stale count",
        not captured,
    )
    doorbell.unread_senders = lambda node: []
    check("mail read since poll injects nothing",
          doorbell.deliver_doorbell("bob", 2) == (doorbell.DELIVER_STALE, "metadata", 0)
          and not captured)
finally:
    doorbell.sqlite3.connect = original_connect
    doorbell.bus.bus_unread_senders = original_bus_unread
    doorbell.unread_senders = original_unread
    doorbell.route_for = original_route
    doorbell.send_to_iterm_session = original_send
    if original_ledger_env is None:
        os.environ.pop("DOORBELL_LEDGER", None)
    else:
        os.environ["DOORBELL_LEDGER"] = original_ledger_env

print(f"\n{'FAIL' if fails else 'ALL PASS'} — {len(fails)} failed")
sys.exit(1 if fails else 0)
