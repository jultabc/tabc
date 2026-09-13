#!/usr/bin/env python3
"""What the listings print for an id has to work when pasted into --id.

The listings shorten the id. Prefix lookup exists so that the shortened form is
still usable — resolve_recipient_message_id() says so in its own docstring. But
the printed value carried a trailing "…", and that character is not part of any
id, so pasting what was on screen matched nothing. The error said "no such
delivery", which reads as a missing message rather than an unusable argument.

A comment asking the next person to keep the two in step would go stale the
first time someone adjusts the format. This pastes the printed value back in.

Hermetic: a temporary store and a stubbed transport; no daemon, no network.
Check the exit code."""

import io
import contextlib
import os
import re
import sys
import tempfile

test_home = tempfile.mkdtemp(prefix="pasteid_")
os.environ["TABC_HOME"] = test_home
os.environ["TABC_DB"] = os.path.join(test_home, "t.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tabus import bus as tabus  # noqa: E402
from tabus import cli as tabc  # noqa: E402

fails = []


def check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        fails.append(name)


con = tabus.connect()
con.executescript(tabus.SCHEMA)
con.commit()
tabus.bus_register(con, "alice", "generic")
tabus.bus_register(con, "bob", "generic")
full_id, _ = tabus.bus_send(con, "alice", ["bob"], "subject", "body")

# The transport is stubbed so the listing renders the real stored id without a
# daemon. Only the printing is under test here.
row = {
    "message_id": full_id,
    "id": full_id,
    "subject": "subject",
    "from": "alice",
    "state": "ACCEPTED",
    "priority": "now",
}
tabc.call = lambda method, path, payload=None, node=None: (200, {"unread": [row]})
tabc._nodekey = lambda: None

printed = {}
for name, run in (
    ("dm", lambda: tabc.fn_mailbox(type("A", (), {"node": "bob", "limit": 20})())),
    ("pull listing", lambda: tabc._print_titles([row])),
):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        run()
    out = buf.getvalue()
    m = re.search(r"id=([^\s)]+)", out)
    check(f"{name}: prints an id", m is not None)
    if not m:
        continue
    shown = m.group(1)
    printed[name] = shown
    # 🔴 The real assertion: hand the printed value straight back to the resolver.
    #    Anything the display appends — an ellipsis, a bracket, a comma — fails here.
    resolved, err = tabus.resolve_recipient_message_id(con, "bob", shown)
    check(f"{name}: printed id resolves ({shown!r})", resolved == full_id and err is None)
    check(f"{name}: printed id is a real prefix", full_id.startswith(shown))

check(
    "both listings shorten to the same length",
    len(set(len(v) for v in printed.values())) == 1,
)
# A full id must keep working; the prefix path is an addition, not a replacement.
check("full id still resolves", tabus.resolve_recipient_message_id(con, "bob", full_id)[0] == full_id)

print("FAIL" if fails else "PASS")
sys.exit(1 if fails else 0)
