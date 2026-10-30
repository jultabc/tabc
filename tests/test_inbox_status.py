#!/usr/bin/env python3
"""inbox status: bus_inbox_status counts what is unread, and where it blocks a send (temporary database).

Contract:
- Unread = ACCEPTED, CLAIMED, INJECTED, counted exactly. READ and PROCESSED are not counted,
  nor mail addressed to another node.
- The oldest message is given per group: not opened (ACCEPTED or CLAIMED) and opened but
  not READ (INJECTED). An old opened message never hides the unopened one.
- Blocking is listed per target, as the send gate counts it: for every listed DM sender and
  tac, a send check to it is refused with the same count; a target not listed is not blocked.
  Broadcasts are counted, and never block.
- The status writes no row and changes no state, in any table.
"""

import os
import sys
import tempfile

os.environ["TABC_DB"] = os.path.join(tempfile.mkdtemp(prefix="ibs_"), "t.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tabus  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(name)


con = tabus.connect()
con.executescript(tabus.SCHEMA)
tabus.migrate(con)
con.commit()
for n in ("erin", "gus", "hal", "ivy", "zed"):
    tabus.bus_register(con, n, "generic")


def tac(name, members):
    tabus.bus_tac_create(con, name)
    t = [row["tac_id"] for row in tabus.bus_tac_list(con) if row.get("name") == name][0]
    for m in members:
        tabus.bus_tac_add(con, t, m)
    return t


def snapshot():
    out = {}
    for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        out[t] = sorted(tuple(r) for r in con.execute(f'SELECT * FROM "{t}"'))
    return out


def status(node="erin"):
    before = snapshot()
    st = tabus.bus_inbox_status(con, node)
    check(f"status of {node} writes nothing", snapshot() == before)
    return st


def gate(to=None, tac_id=None):
    """The send gate's own verdict for erin, without sending."""
    mid, info = tabus.bus_send(con, "erin", to or [], "s", "b", tac_id=tac_id, check_only=True)
    if mid == tabus.SEND_CHECK_PASSED:
        return None
    return getattr(info, "details", {})


def deliver(sender, to, subject, tac_id=None, broadcast=False):
    mid, _ = tabus.bus_send(con, sender, to, subject, "x", tac_id=tac_id, broadcast=broadcast)
    return mid


def set_state(mid, node, state):
    con.execute("UPDATE deliveries SET state=? WHERE message_id=? AND recipient_id=?", (state, mid, node))
    con.commit()


# 1. Empty.
st = status()
check("empty: zero everywhere", st["unread"] == 0 and st["oldest"] == {"unopened": None, "opened_not_read": None}
      and st["blocking"] == {"dm": [], "tac": []})

# 2. An old opened (INJECTED) message, then a new unopened one: each group has its own oldest (jack 59d76f98).
old = deliver("gus", ["erin"], "old-opened")
set_state(old, "erin", "INJECTED")
new = deliver("hal", ["erin"], "new-unopened")
st = status()
check("groups: unopened oldest is the ACCEPTED one", st["oldest"]["unopened"]["subject"] == "new-unopened")
check("groups: opened oldest is the INJECTED one", st["oldest"]["opened_not_read"]["subject"] == "old-opened")
check("counts by state", st["by_state"] == {"ACCEPTED": 1, "CLAIMED": 0, "INJECTED": 1} and st["unread"] == 2)

# 3. READ and PROCESSED are not unread; another node's mail is not counted.
r1 = deliver("ivy", ["erin"], "read-one")
set_state(r1, "erin", "READ")
r2 = deliver("ivy", ["erin"], "processed-one")
set_state(r2, "erin", "PROCESSED")
deliver("gus", ["zed"], "to-zed")
st = status()
check("READ, PROCESSED and other nodes' mail not counted", st["unread"] == 2, str(st["by_state"]))

# 4. A pulled but unopened message (CLAIMED) is unopened and blocks.
tabus.claim_new(con, "erin", limit=10)
st = status()
check("CLAIMED counts as not opened", st["by_state"]["CLAIMED"] == 1 and st["by_state"]["ACCEPTED"] == 0)

# 5. Tacs: unread in tac A only. A is listed and blocked; tac B is not.
ta = tac("alpha", ["erin", "gus", "hal"])
tb = tac("beta", ["erin", "hal"])
deliver("gus", [], "in-alpha-1", tac_id=ta)
tabus.bus_tac_mark_read(con, "hal", ta)  # hal reads first, or the gate refuses hal's send
check("setup: hal can send to alpha", deliver("hal", [], "in-alpha-2", tac_id=ta) is not None)
st = status()
listed_tacs = {t["tac"]: t for t in st["blocking"]["tac"]}
check("alpha listed with 2, by name", ta in listed_tacs and listed_tacs[ta]["unread"] == 2 and listed_tacs[ta]["name"] == "alpha")
check("beta not listed", tb not in listed_tacs)

# 6. Broadcast: counted by kind, never a blocking target.
deliver("ivy", [], "announce", broadcast=True)
st = status()
check("broadcast counted by kind", st["by_kind"]["broadcast"] == 1)
check("broadcast sender not a dm blocking target", all(d["node"] != "ivy" for d in st["blocking"]["dm"]))

# 7. Every listed target matches the gate with the same count; unlisted targets pass.
st = status()
for d in st["blocking"]["dm"]:
    g = gate(to=[d["node"]])
    got = {x["node"]: x["unread"] for x in (g or {}).get("recipients", [])}
    check(f"dm to {d['node']}: gate refuses with {d['unread']}", got.get(d["node"]) == d["unread"], str(g))
for t in st["blocking"]["tac"]:
    g = gate(tac_id=t["tac"])
    check(f"tac {t['name']}: gate refuses with {t['unread']}", (g or {}).get("unread") == t["unread"], str(g))
listed_nodes = {d["node"] for d in st["blocking"]["dm"]}
for n in ("ivy", "zed"):
    check(f"dm to {n} (not listed): gate passes", n not in listed_nodes and gate(to=[n]) is None)
check("tac beta (not listed): gate passes", gate(tac_id=tb) is None)

# 8. Opening releases: the target drops out of the list and the gate passes.
tabus.bus_tac_mark_read(con, "erin", ta)
st = status()
check("after opening alpha: not listed", all(t["tac"] != ta for t in st["blocking"]["tac"]))
check("after opening alpha: gate passes", gate(tac_id=ta) is None)

# 9. One snapshot (coco 8709e174): another connection marks the only unopened DM READ
#    right after the first query. Every part of the answer must still see it.
for n in ("kim", "lee"):
    tabus.bus_register(con, n, "generic")
race = deliver("lee", ["kim"], "race")


class Interleave:
    """Forwards to the real connection; after the first query, a second connection writes."""

    def __init__(self, real):
        self.real, self.fired = real, False

    def __getattr__(self, name):
        return getattr(self.real, name)

    def execute(self, sql, *args):
        cur = self.real.execute(sql, *args)
        if not self.fired and "GROUP BY d.state" in sql:
            self.fired = True
            rows = cur.fetchall()
            other = tabus.connect()
            other.execute("UPDATE deliveries SET state='READ' WHERE message_id=? AND recipient_id='kim'", (race,))
            other.commit()
            other.close()
            return iter(rows)
        return cur


px = Interleave(con)
st = tabus.bus_inbox_status(px, "kim")
check("race: the write happened between queries", px.fired
      and con.execute("SELECT state FROM deliveries WHERE message_id=? AND recipient_id='kim'", (race,)).fetchone()[0] == "READ")
unopened = st["by_state"]["ACCEPTED"] + st["by_state"]["CLAIMED"]
check(
    "race: counts, oldest and blocking come from one moment",
    unopened == 1 and st["oldest"]["unopened"] is not None and st["blocking"]["dm"] == [{"node": "lee", "unread": 1}],
    str(st),
)
check("race: the snapshot is closed afterwards", not con.in_transaction)

# 10. Program node (jack 9d72d474 F1): DMs never block its sends; its tac still does.
tabus.bus_register(con, "bot", "generic")
tp = tac("ptac", ["bot", "gus"])
deliver("gus", ["bot"], "dm-to-bot")
deliver("gus", [], "tac-to-bot", tac_id=tp)
con.execute("UPDATE nodes SET is_program=1 WHERE node_id='bot'")
con.commit()
st = status("bot")
check("program: no dm blocking, flagged", st["program"] is True and st["blocking"]["dm"] == [])
check("program: tac blocking kept", [t["tac"] for t in st["blocking"]["tac"]] == [tp])
mid, _ = tabus.bus_send(con, "bot", ["gus"], "s", "b", check_only=True)
check("program: the gate agrees, dm to gus passes", mid == tabus.SEND_CHECK_PASSED)
mid, info = tabus.bus_send(con, "bot", [], "s", "b", tac_id=tp, check_only=True)
check("program: the gate agrees, ptac refuses", mid is None and getattr(info, "code", None) == "UNREAD_BLOCKED")

# 11. Closed tac (jack 9d72d474 N2): listed as closed.
tc = tac("gone", ["erin", "gus"])
deliver("gus", [], "in-gone", tac_id=tc)
tabus.bus_tac_close(con, tc, "done", "gus")
st = status()
g = [t for t in st["blocking"]["tac"] if t["tac"] == tc]
check("closed tac flagged", g and g[0]["closed"] is True, str(g))

# 12. Screen lines: a closed tac is not in the "refused until opened" line; a program node says so.
from tabus.cli import _status_lines  # noqa: E402

lines = "\n".join(_status_lines({
    "node": "x", "checked_at": "t", "unread": 2, "program": True,
    "by_state": {"ACCEPTED": 2, "CLAIMED": 0, "INJECTED": 0}, "by_kind": {"dm": 0, "tac": 2, "broadcast": 0},
    "oldest": {"unopened": None, "opened_not_read": None},
    "blocking": {"dm": [], "tac": [{"tac": "a", "name": "live", "unread": 1, "closed": False},
                                   {"tac": "b", "name": "gone", "unread": 1, "closed": True}]},
}))
refused = [l for l in lines.splitlines() if "refused until" in l]
check("screen: closed tac kept out of the refused line", refused and "live" in refused[0] and "gone" not in refused[0], lines)
check("screen: closed tac shown apart", "unread in closed tacs" in lines and "gone (1)" in lines, lines)
check("screen: program node line", "program node: DMs never block its sends" in lines, lines)

print(f"\n=== Results: FAIL {len(fails)} ===")
sys.exit(1 if fails else 0)
