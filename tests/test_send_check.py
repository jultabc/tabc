#!/usr/bin/env python3
"""send check: bus_send(check_only=True) reaches the same verdict as a real send and writes nothing (temporary database).

Contract:
- For every case the check's verdict (refused or not, and the refusal code) equals what a real send returns.
- A check writes no row and changes no state, in any table.
- An UNREAD_BLOCKED refusal lists the blocking messages: id, from, subject, accepted_at, oldest first,
  at most BLOCKING_LIST_MAX, with `more` for the rest. The older fields stay as they were.
- The list only ever shows deliveries addressed to the sender.
- A pulled but unopened delivery (CLAIMED) still blocks; opening it releases the block.
"""

import os
import sys
import tempfile

os.environ["TABC_DB"] = os.path.join(tempfile.mkdtemp(prefix="sck_"), "t.db")
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
for n in ("erin", "alice", "frank", "gus"):
    tabus.bus_register(con, n, "generic")
tabus.bus_tac_create(con, "sq")
sq = [row["tac_id"] for row in tabus.bus_tac_list(con) if row.get("name") == "sq"][0]
for n in ("erin", "alice", "frank"):
    tabus.bus_tac_add(con, sq, n)


def snapshot():
    """Every row of every table, so a check that writes anything shows up."""
    out = {}
    for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        out[t] = sorted(tuple(r) for r in con.execute(f'SELECT * FROM "{t}"'))
    return out


def verdict(mid, info):
    return ("sent" if mid is not None else "refused", getattr(info, "code", None))


def compare(name, sender, to, subject="s", body="b", **kw):
    """Check first, then really send with the same arguments; the two must agree."""
    before = snapshot()
    cmid, cinfo = tabus.bus_send(con, sender, to, subject, body, check_only=True, **kw)
    after = snapshot()
    check(f"{name}: the check writes nothing", before == after)
    rmid, rinfo = tabus.bus_send(con, sender, to, subject, body, **kw)
    cv = ("sent" if cmid == tabus.SEND_CHECK_PASSED else "refused", getattr(cinfo, "code", None))
    check(f"{name}: check {cv} = send {verdict(rmid, rinfo)}", cv == verdict(rmid, rinfo))
    if cmid is None:
        check(f"{name}: the refusal sentence is the same", str(cinfo) == str(rinfo))
    return cmid, cinfo, rmid, rinfo


# 1. Nothing unread: not blocked, and the check names the recipients.
c, ci, _, _ = compare("tac, nothing unread", "erin", [], tac_id=sq)
check("tac, nothing unread: recipients listed", c == tabus.SEND_CHECK_PASSED and sorted(ci["recipients"]) == ["alice", "frank"])

# 2. alice has erin's message unread in sq: blocked, and the list names it.
c, ci, _, _ = compare("tac, one unread", "alice", [], tac_id=sq)
d = getattr(ci, "details", {})
check(
    "tac block lists the message",
    d.get("unread") == 1 and d.get("more") == 0 and [m["from"] for m in d.get("messages", [])] == ["erin"]
    and d["messages"][0]["subject"] == "s" and d.get("scope") == "tac",
)

# 3. Pulled but not opened: still blocked. Opening releases it.
claimed, _q, _l = tabus.claim_new(con, "alice", limit=10)
check("alice's pull claimed the tac message", any(m["id"] == d["messages"][0]["id"] for m in claimed))
c, ci, _, _ = compare("tac, pulled not opened", "alice", [], tac_id=sq)
check("pulled not opened still blocks", c is None and ci.code == "UNREAD_BLOCKED")
tabus.bus_tac_mark_read(con, "alice", sq)
c, _, _, _ = compare("tac, after open", "alice", [], tac_id=sq)
check("opened: not blocked", c == tabus.SEND_CHECK_PASSED)

# 4. DM: gus sends erin two DMs; erin to gus is blocked, erin to frank is not.
tabus.bus_send(con, "gus", ["erin"], "g1", "x")
tabus.bus_send(con, "gus", ["erin"], "g2", "x")
c, ci, _, _ = compare("dm, recipient has unread to sender", "erin", ["gus"])
d = getattr(ci, "details", {})
check(
    "dm block lists both, oldest first",
    [m["subject"] for m in d.get("messages", [])] == ["g1", "g2"] and d.get("recipients") == [{"node": "gus", "unread": 2}],
)
compare("dm, unread only from another node", "erin", ["frank"])

# 5. The list shows only mail addressed to the sender: frank's unread from gus never appears for erin.
tabus.bus_send(con, "gus", ["frank"], "secret-to-frank", "x")
c, ci, _, _ = compare("dm, other node's mail not listed", "erin", ["gus"])
check(
    "no other node's subject in the list",
    all(m["subject"] != "secret-to-frank" for m in getattr(ci, "details", {}).get("messages", [])),
)

# 6. Cap: twelve unread from gus to frank; the list stops at BLOCKING_LIST_MAX, more carries the rest.
for i in range(11):
    tabus.bus_send(con, "gus", ["frank"], f"f{i}", "x")
c, ci, _, _ = compare("dm, over the list cap", "frank", ["gus"])
d = getattr(ci, "details", {})
check(
    "cap and more",
    len(d.get("messages", [])) == tabus.BLOCKING_LIST_MAX and d.get("more") == 12 - tabus.BLOCKING_LIST_MAX
    and d["messages"][0]["subject"] == "secret-to-frank",
)

# 6b. Two blocking recipients: the list is oldest first across both, before the cap.
#     hal sends first, ivy second, then hal again; ivy's sits between hal's two.
for n in ("hal", "ivy"):
    tabus.bus_register(con, n, "generic")
tabus.bus_send(con, "hal", ["erin"], "h-old", "x")
tabus.bus_send(con, "ivy", ["erin"], "i-mid", "x")
tabus.bus_send(con, "hal", ["erin"], "h-new", "x")
c, ci, _, _ = compare("dm, two recipients", "erin", ["ivy", "hal"])
check(
    "two recipients: oldest first across both",
    [m["subject"] for m in getattr(ci, "details", {}).get("messages", [])] == ["h-old", "i-mid", "h-new"],
)
#     Over the cap: ivy's single old message must stay in the list even though hal has eleven newer ones.
tabus.bus_register(con, "jo", "generic")
tabus.bus_send(con, "ivy", ["jo"], "ivy-oldest", "x")
for i in range(11):
    tabus.bus_send(con, "hal", ["jo"], f"hal{i}", "x")
c, ci, _, _ = compare("dm, two recipients over the cap", "jo", ["hal", "ivy"])
d = getattr(ci, "details", {})
check(
    "two recipients over the cap: oldest kept, more counts the rest",
    d.get("messages", [{}])[0].get("subject") == "ivy-oldest" and len(d["messages"]) == tabus.BLOCKING_LIST_MAX
    and d.get("more") == 12 - tabus.BLOCKING_LIST_MAX,
)

# 7. Broadcast is never blocked by unread mail.
compare("broadcast", "erin", [], broadcast=True)

# 8. Other refusals reach the same verdict.
compare("body over the limit", "erin", ["frank"], body="x" * (tabus.MAX_BODY_BYTES + 1))
compare("unregistered recipient", "erin", ["nobody"])
compare("unregistered sender", "nobody", ["erin"])
tabus.bus_tac_create(con, "old")
old = [row["tac_id"] for row in tabus.bus_tac_list(con) if row.get("name") == "old"][0]
tabus.bus_tac_add(con, old, "erin")
tabus.bus_tac_add(con, old, "alice")
tabus.bus_tac_close(con, old, "done", "erin")
compare("closed tac", "erin", [], tac_id=old)

# 9. Older fields of the refusal stay.
_, ci = tabus.bus_send(con, "erin", ["gus"], "s", "b", check_only=True)
d = getattr(ci, "details", {})
check("older dm fields kept", d.get("scope") == "dm" and "recipients" in d and ci.retry == "after_condition")

print(f"\n=== Results: FAIL {len(fails)} ===")
sys.exit(1 if fails else 0)
