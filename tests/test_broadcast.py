#!/usr/bin/env python3
"""Broadcast to all eligible non-program nodes; verify broadcast and has_broadcast using a temporary database.

Current contract: broadcast reaches all eligible nodes.
- broadcast=True targets every non-program node except the sender and removed nodes. is_team is reserved and ignored.
- Program nodes are send-only and reject delivery; exclude them in recipient selection to avoid rejecting the whole envelope.
- Removed nodes are excluded.
- Combining broadcast and tac is rejected. scan_unread includes has_broadcast for SHADOW override.
- Zero recipients after excluding the sender is an explicit rejection, not silent success.
- Team-scoped broadcast is a future feature; is_team is currently not read.
"""

import os
import sys
import tempfile

os.environ["TABC_DB"] = os.path.join(tempfile.mkdtemp(prefix="bcast_"), "t.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tabus  # noqa: E402
from tabus import doorbell_poll as doorbell_poll  # noqa: E402

fails = []


def check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        fails.append(name)


con = tabus.connect()
con.executescript(tabus.SCHEMA)
tabus.migrate(con)  # Deployment migration also adds is_program, is_team, and broadcast columns.
con.commit()

# Four ordinary nodes, one program node (prog), and one node to remove (gone).
for n in ("erin", "alice", "bob", "zoe"):
    tabus.bus_register(con, n, "generic")
okp, _ = tabus.bus_register(con, "prog", "engine", program=True)
check("program node registration succeeds", okp)
tabus.bus_register(con, "gone", "generic")


def recips_of(mid):
    return {
        r["recipient_id"]
        for r in con.execute(
            "SELECT recipient_id FROM deliveries WHERE message_id=?", (mid,)
        )
    }


# 1. Broadcast reaches everyone except the program and sender: alice, bob, zoe, and gone.
mid, _ = tabus.bus_send(con, "erin", [], '\uacf5\uc9c0', '\uc804\uccb4 \ubcf8\ubb38', broadcast=True)
check("broadcast sending succeeds", mid is not None)
recips = recips_of(mid)
check("all ordinary nodes receive it (alice, bob, zoe, gone)", recips == {"alice", "bob", "zoe", "gone"})
check("sender erin is excluded", "erin" not in recips)
check("program node prog is excluded", "prog" not in recips)
bcr = con.execute("SELECT broadcast, tac_id FROM messages WHERE id=?", (mid,)).fetchone()
check("message.broadcast=1", bcr["broadcast"] == 1)
check("broadcast tac_id is NULL (individual delivery eligible for alarms)", bcr["tac_id"] is None)

# 2. Reject broadcast combined with tac to avoid ambiguous semantics.
m2, i2 = tabus.bus_send(con, "erin", [], "x", "y", tac_id="team", broadcast=True)
check("broadcast combined with tac is rejected", m2 is None and "mutually exclusive" in (i2 or ""))

# 3. Reserved is_team does not affect broadcast recipients, whether enabled or disabled.
tabus.bus_set_team(con, "alice", True)  # Enabled.
tabus.bus_set_team(con, "bob", False)  # Disabled (default).
mid3, _ = tabus.bus_send(con, "erin", [], '\uacf5\uc9c0t', '\ubcf8\ubb38t', broadcast=True)
check("is_team is irrelevant: alice (1) and bob (0) both receive it", {"alice", "bob"} <= recips_of(mid3))

# 4. Exclude removed nodes.
tabus.bus_remove(con, "gone")
mid4, _ = tabus.bus_send(con, "erin", [], '\uacf5\uc9c0r', '\ubcf8\ubb38r', broadcast=True)
check("removed node gone is excluded", "gone" not in recips_of(mid4))
check("all remaining eligible nodes still receive it", {"alice", "bob", "zoe"} <= recips_of(mid4))

# 5. Test both values of scan_unread has_broadcast, not just one direction.
# late registers after the broadcast and receives only a DM, so has_broadcast is False.
# alice received the broadcast and still has True.
tabus.bus_register(con, "late", "generic")
tabus.bus_send(con, "erin", ["late"], "dm only", "body")
rows = {r[0]: r for r in doorbell_poll.scan_unread(con)}
check("late has_broadcast=False (DM only; unregistered before broadcast)", rows["late"][4] is False)
check("alice has_broadcast=True (broadcast included)", rows["alice"][4] is True)

# 6. Ordinary DMs retain broadcast=0 (existing send behavior regression test).
m6, _ = tabus.bus_send(con, "erin", ["alice"], '\ubcf4\ud1b5', "dm")
r6 = con.execute("SELECT broadcast FROM messages WHERE id=?", (m6,)).fetchone()
check("ordinary DM has broadcast=0", r6["broadcast"] == 0)

# 7. Zero recipients after excluding the sender must be an explicit rejection.
for n in ("alice", "bob", "zoe", "late"):
    tabus.bus_remove(con, n)  # erin, the sender, is now the only non-program, non-removed node.
m7, i7 = tabus.bus_send(con, "erin", [], '\uacf5\uc9c0\ub05d', '\ubcf8\ubb38\ub05d', broadcast=True)
check(
    "zero recipients causes explicit rejection, not silence",
    m7 is None and ("no valid recipients" in (i7 or "") or "refused" in (i7 or "")),
)

print("\n" + ("✅ ALL PASS" if not fails else f"❌ {len(fails)} failed: {fails}"))
sys.exit(1 if fails else 0)
