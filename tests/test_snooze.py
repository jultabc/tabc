#!/usr/bin/env python3
"""Snooze: suppress all alarms for N minutes, including broadcasts, with automatic expiry (temporary database).

Contract:
- Nodes with snooze_until > now are excluded from alarm candidates, including broadcasts.
- Broadcasts still reach the inbox, but snoozed nodes do not ring: notification and delivery are distinct.
- Expiry automatically restores eligibility on each poll; off/0 clears snooze immediately.
- Store UTC ISO timestamps and compare through parse_instant, never lexicographically (Hu's finding).
"""

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["TABC_DB"] = os.path.join(tempfile.mkdtemp(prefix="snooze_"), "t.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tabus  # noqa: E402
from tabus import doorbell_poll as doorbell_poll  # noqa: E402

fails = []


def check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        fails.append(name)


def candidates():
    return {r[0] for r in doorbell_poll.scan_unread(tabus.connect())}


con = tabus.connect()
con.executescript(tabus.SCHEMA)
tabus.migrate(con)
con.commit()
# Unread messages create alarm candidates independently of snooze.
# Broadcast reaches all non-program nodes, so no team configuration is needed for recipients.
for n in ("erin", "alice", "bob"):
    tabus.bus_register(con, n, "generic")

tabus.bus_send(con, "bob", ["erin"], "s", "b")
tabus.bus_send(con, "bob", ["alice"], "s", "b")
check("initially erin and alice are both candidates", {"erin", "alice"} <= candidates())

# Snooze erin for 60 minutes: suppress all alarms.
ok, _ = tabus.bus_set_snooze(con, "erin", 60)
check("set_snooze(erin, 60) succeeds", ok)
until, remain = tabus.bus_get_snooze(con, "erin")
check("remaining duration is about 60 minutes (3300-3600 seconds)", 3300 <= remain <= 3600)
check("snoozed erin is excluded from candidates", "erin" not in candidates())
check("alice remains eligible (only one node is snoozed)", "alice" in candidates())

# Broadcast must not ring a snoozed node, although delivery still occurs.
mid, _ = tabus.bus_send(
    con, "bob", [], '\uacf5\uc9c0', "b", broadcast=True
)  # All non-program nodes except sender bob: erin and alice.
delivered = con.execute(
    "SELECT 1 FROM deliveries WHERE message_id=? AND recipient_id='erin'", (mid,)
).fetchone()
check("broadcast is delivered to erin", delivered is not None)
check(
    "snoozed erin remains excluded after broadcast", "erin" not in candidates()
)

# Expiry automatically restores eligibility.
past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec="seconds")
con.execute("UPDATE nodes SET snooze_until=? WHERE node_id='erin'", (past,))
con.commit()
check("expired snooze automatically restores erin as a candidate", "erin" in candidates())

# off (0) clears snooze immediately.
tabus.bus_set_snooze(con, "erin", 30)
check("re-snoozing excludes the node again", "erin" not in candidates())
tabus.bus_set_snooze(con, "erin", 0)
u2, r2 = tabus.bus_get_snooze(con, "erin")
check("off (0) clears snooze to None", u2 is None and r2 == 0)
check("clearing snooze restores erin as a candidate", "erin" in candidates())

# Guard against unregistered nodes.
ok2, msg2 = tabus.bus_set_snooze(con, "ghost", 60)
check("set_snooze rejects an unregistered node", not ok2 and "unregistered" in (msg2 or ""))
check("get_snooze returns None for an unregistered node", tabus.bus_get_snooze(con, "ghost") is None)

print("\n" + ("✅ ALL PASS" if not fails else f"❌ {len(fails)} failed: {fails}"))
sys.exit(1 if fails else 0)
