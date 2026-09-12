#!/usr/bin/env python3
"""read-before-send: reject sending while relevant messages remain unread (temporary database).

TAC contract:
- A member with unread ACCEPTED/CLAIMED deliveries in a TAC cannot send to that TAC until caught up.
- bus_tac_mark_read(member, tac) marks unread deliveries INJECTED and releases the guard.
- The guard is reciprocal: recipients must read prior messages before replying.
- Non-members and unregistered observers have no TAC deliveries, so mark_read returns 0 without side effects.

DM contract (per recipient):
- Before sending a DM, reject if the recipient has sent an unread DM to the sender. Read that conversation first.
- Only that recipient's messages matter; other senders' unread messages do not block the DM.
- TAC messages (m.tac_id NOT NULL) and broadcasts (m.broadcast=1) are outside the DM guard.
- Opening a DM (to INJECTED) releases the guard. Broadcast sending itself is exempt so announcements can go out.
"""

import os
import sys
import tempfile

os.environ["TABC_DB"] = os.path.join(tempfile.mkdtemp(prefix="rbs_"), "t.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tabus  # noqa: E402

fails = []


def check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        fails.append(name)


con = tabus.connect()
con.executescript(tabus.SCHEMA)
tabus.migrate(con)
con.commit()
for n in ("erin", "alice", "frank"):
    tabus.bus_register(con, n, "generic")
tabus.bus_tac_create(con, "sq")
for n in ("erin", "alice", "frank"):
    tabus.bus_tac_add(con, "sq", n)

# 1. erin has no unread messages in sq and can send; alice and frank receive it.
m1, i1 = tabus.bus_send(con, "erin", [], "s1", "b1", tac_id="sq")
check("erin sends successfully with no unread messages", m1 is not None)

# 2. Reject alice's send because erin's s1 is unread.
m2, i2 = tabus.bus_send(con, "alice", [], "s2", "b2", tac_id="sq")
check(
    "alice cannot send while messages remain unread",
    m2 is None and "Read them first" in (i2 or "") and "unread" in (i2 or ""),
)

# 3. alice catches up with mark_read, releasing the guard and allowing sending.
n = tabus.bus_tac_mark_read(con, "alice", "sq")
check("alice marks one message (s1) read", n == 1)
m3, i3 = tabus.bus_send(con, "alice", [], "s3", "b3", tac_id="sq")
check("alice can send after reading", m3 is not None)

# 4. Reciprocal guard: erin has not read alice's s3 and is now blocked too.
m4, i4 = tabus.bus_send(con, "erin", [], "s4", "b4", tac_id="sq")
check("reciprocal guard blocks erin until alice's s3 is read", m4 is None and "Read them first" in (i4 or ""))
tabus.bus_tac_mark_read(con, "erin", "sq")
m5, _ = tabus.bus_send(con, "erin", [], "s5", "b5", tac_id="sq")
check("erin can send after reading", m5 is not None)

# 5. Non-member/unregistered observers mark zero messages read, without side effects.
check(
    "non-member mark_read returns 0 without observer side effects",
    tabus.bus_tac_mark_read(con, "ghost", "sq") == 0,
)

# DM extension: per-recipient guard.
tabus.bus_register(con, "kim", "generic")  # Control node with no prior DM conversation with erin.

# 6. Unread TAC deliveries do not affect the DM guard (m.tac_id IS NULL).
# frank has unread sq TAC deliveries but no unread DM from erin, so frank can DM erin.
mdm, _ = tabus.bus_send(con, "frank", ["erin"], "dm0", "b")
check("unread TAC messages do not block DM: frank can send to erin", mdm is not None)

# 7. alice can DM erin with no unread DMs; erin then has one unread DM from alice.
mc, _ = tabus.bus_send(con, "alice", ["erin"], "dm1", "b")
check("alice can DM erin with no unread DMs", mc is not None)

# 8. Reject erin's DM to alice until alice's dm1 is read.
md, im = tabus.bus_send(con, "erin", ["alice"], "dm2", "b")
check(
    "erin cannot DM alice before reading alice's message",
    md is None and "read their messages first" in (im or "") and "alice" in (im or ""),
)

# 9. Per-recipient scope: erin can DM kim, who has never sent a DM; unread alice/frank messages do not matter.
mk, _ = tabus.bus_send(con, "erin", ["kim"], "dm3", "b")
check("per-recipient guard allows erin to DM kim despite other unread messages", mk is not None)

# 10. erin opens alice's dm1 (ACCEPTED to CLAIMED to INJECTED), releasing the guard and allowing a DM to alice.
tabus.reopen_unread(con, "erin", message_id=mc)  # ACCEPTED to CLAIMED: open the body.
tabus.bus_ack(con, "erin", mc, "INJECTED")  # CLAIMED to INJECTED: open records delivery.
md2, _ = tabus.bus_send(con, "erin", ["alice"], "dm4", "b")
check("erin can DM alice after reading alice's message", md2 is not None)

# 11. Broadcast sending is exempt so announcements can go out, even though alice has unread dm4 from erin.
# The not-broadcast guard bypass allows sending; a normal DM to erin would be blocked.
# Broadcasts reach all non-program nodes, so no team selection is needed to have recipients.
mbc, _ = tabus.bus_send(con, "alice", [], "bc", "b", broadcast=True)
check("broadcast bypasses the guard despite unread messages", mbc is not None)

# 12. Unread broadcasts (m.broadcast=1) are outside the DM guard, including alice's broadcast to erin.
# erin can DM alice because dm1 was opened above and broadcasts are outside the conversation scope.
mb, _ = tabus.bus_send(con, "erin", ["alice"], "dm5", "b")
check("unread broadcasts do not block DM: erin can send to alice", mb is not None)

print("\n" + ("✅ ALL PASS" if not fails else f"❌ {len(fails)} failed: {fails}"))
sys.exit(1 if fails else 0)
