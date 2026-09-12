#!/usr/bin/env python3
"""Node removal: bus_remove unit tests.

Contract: default soft removal plus optional purge.
- purge=False: mark removed_nodes and hide from who; retain nodes and history for restoration.
- purge=True: delete permanently only when sent, received, and route history are all empty. Reject otherwise to prevent irreversible mistakes.
Uses a temporary database, never live data. Run directly and check the exit code."""

import os
import sys
import tempfile

os.environ["TABC_DB"] = os.path.join(tempfile.mkdtemp(prefix="noderm_"), "t.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tabus  # noqa: E402

fails = []


def check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        fails.append(name)


con = tabus.connect()
con.executescript(tabus.SCHEMA)
con.commit()
for n in ["alice", "bob", "carol"]:
    tabus.bus_register(con, n, "generic")
# Create history: alice sends, carol receives; bob has no history.
tabus.bus_send(con, "alice", ["carol"], "s", "b")
con.commit()


def in_who(node):
    return any(r["node_id"] == node for r in tabus.bus_who_rows(con))


def in_nodes(node):
    return (
        con.execute("SELECT 1 FROM nodes WHERE node_id=?", (node,)).fetchone()
        is not None
    )


def in_removed(node):
    return (
        con.execute("SELECT 1 FROM removed_nodes WHERE node_id=?", (node,)).fetchone()
        is not None
    )


# 1. Purge bob, whose history is empty.
ok, msg = tabus.bus_remove(con, "bob", purge=True)
check("1 purge succeeds with no history", ok)
check("1 bob is removed from nodes", not in_nodes("bob"))
check("1 bob is absent from who", not in_who("bob"))

# 2. Reject purging alice, who has sent history, to prevent mistakes.
ok, msg = tabus.bus_remove(con, "alice", purge=True)
check("2 purge with history is rejected", not ok)
check("2 rejection reason mentions history or refusal", ("history" in msg) or ("refused" in msg))
check("2 alice remains in nodes", in_nodes("alice"))

# 3. Soft-remove carol with received history: mark removed_nodes and retain nodes.
ok, msg = tabus.bus_remove(con, "carol", purge=False)
check("3 soft removal succeeds", ok)
check("3 carol is in removed_nodes", in_removed("carol"))
check("3 carol remains in nodes (history preserved)", in_nodes("carol"))

# 4. who hides soft-removed carol but still shows alice.
check("4 carol is hidden from who", not in_who("carol"))
check("4 alice is visible in who", in_who("alice"))

# 5. Removing the removed_nodes entry restores visibility in who.
con.execute("DELETE FROM removed_nodes WHERE node_id='carol'")
con.commit()
check("5 restoration returns carol to who", in_who("carol"))

# 6. Soft removal is idempotent: both calls succeed and leave one removed_nodes row.
tabus.bus_remove(con, "carol", purge=False)
ok, msg = tabus.bus_remove(con, "carol", purge=False)
cnt = con.execute(
    "SELECT COUNT(*) c FROM removed_nodes WHERE node_id='carol'"
).fetchone()["c"]
check("6 soft removal is idempotent (second call succeeds)", ok)
check("6 removed_nodes has no duplicate (one row)", cnt == 1)

# 7. Reject an unregistered node.
ok, msg = tabus.bus_remove(con, "nope", purge=False)
check("7 unregistered node is rejected", not ok and "unregistered" in msg)

# 8. Reject an empty node with a normal error.
ok, msg = tabus.bus_remove(con, "", purge=True)
check("8 empty node is rejected", not ok)

# 9. Purged nodes must not remain in removed_nodes either.
check("9 purged bob is absent from removed_nodes", not in_removed("bob"))

# The retired local who command has no separate store-reading path.
# The daemon uses bus_who_rows, whose removal behavior is checked above.

# 12. BLOCK-B: mailbox_opens has no FK, but stale rows could attach old history to a re-registered node.
# Permanent removal must clean up everything its contract promises.
con.execute(
    "CREATE TABLE IF NOT EXISTS mailbox_opens(node TEXT PRIMARY KEY, opened_at TEXT NOT NULL)"
)
tabus.bus_register(con, "evemail", "generic")
con.execute("INSERT INTO mailbox_opens(node, opened_at) VALUES('evemail','x')")
con.commit()
tabus.bus_remove(con, "evemail", purge=True)
check(
    "12 purge leaves no mailbox_opens rows",
    con.execute("SELECT COUNT(*) c FROM mailbox_opens WHERE node='evemail'").fetchone()[
        "c"
    ]
    == 0,
)

# 13. Complete deactivation: all active-node operations reject soft-removed nodes.
# carol was soft-removed in test 6; register, who, and send must agree.
ok, msg = tabus.bus_register(con, "carol", "generic")
check("13 registration rejects removed nodes", (not ok) and "removed" in msg)
mid, info = tabus.bus_send(con, "alice", ["carol"], "s", "b")
check("13 direct send to a removed node has no valid recipient (no delivery)", mid is None)
check("13 beat rejects removed nodes", not tabus.bus_beat(con, "carol"))
_msgs, _q, _l = tabus.claim_new(con, "carol")
check("13 pull returns no results for removed nodes", _msgs == [])

# 14. Restore reactivates the node: who shows it and beat/register resume.
ok, msg = tabus.bus_restore(con, "carol", by="tester")
check("14 restore succeeds", ok)
check("14 restored node reappears in who", in_who("carol"))
check("14 beat resumes after restore", tabus.bus_beat(con, "carol"))

# 15. The removals audit records soft removal, purge, restoration, and the actor (by).
rows = con.execute(
    "SELECT node_id, mode, removed_by FROM removals ORDER BY id"
).fetchall()
modes = [(r["node_id"], r["mode"]) for r in rows]
check("15 audit records carol restoration by tester", ("carol", "restore") in modes)
check(
    "15 audit includes soft removal and purge",
    any(m[1] == "soft" for m in modes) and any(m[1] == "purge" for m in modes),
)
check(
    "15 restore row has by=tester",
    any(r["mode"] == "restore" and r["removed_by"] == "tester" for r in rows),
)

print(f"\n{'❌ FAIL' if fails else '✅ ALL PASS'} — {len(fails)} failed")
sys.exit(1 if fails else 0)
