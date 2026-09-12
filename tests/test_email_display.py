#!/usr/bin/env python3
"""Alarm sender email: bus_unread_senders owner_email, migration, and graceful compatibility.

owner_email is a self-reported user email, like git config. owner_email_verified is independent of sender verification.
Notifier code and tabd schema may deploy separately; reading an older database without the column must not crash (2026-08-28 regression).
Temporary databases only, never live data. Run directly and check the exit code."""

import os
import sys
import tempfile

os.environ["TABC_DB"] = os.path.join(tempfile.mkdtemp(prefix="email_"), "t.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 🔴 the module, not the package. This file reaches into the implementation —
#    it replaces functions on it, or reads names that begin with an underscore.
#    Re-export copies public names only, and an attribute set on the package is
#    invisible to callers inside the module.
from tabus import bus as tabus  # noqa: E402

fails = []


def check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        fails.append(name)


# Part A: SCHEMA includes owner_email; bus_register stores it and unread_senders returns it.
print("Part A: current database with the column")
con = tabus.connect()
con.executescript(tabus.SCHEMA)
con.commit()
check("A0 SCHEMA nodes includes owner_email", tabus._nodes_has_owner_email(con))

tabus.bus_register(con, "alice", "generic", email="alice@example.com")
tabus.bus_register(con, "bob", "generic")  # Node without an email.
tabus.bus_register(con, "carol", "generic")  # Fresh recipient for the sender without an email.
con.commit()
tabus.bus_send(con, "alice", ["bob"], "s1", "b1")  # alice (with email) sends to bob.
# bob sends to carol, not alice: the per-recipient read-before-send guard allows this because no carol DM is unread.
# A send to alice would be blocked by unread s1; the guard was correct and the old test was stale.
tabus.bus_send(con, "bob", ["carol"], "s2", "b2")  # bob (without email) sends to carol.
con.commit()

sb = tabus.bus_unread_senders(con, "bob")  # bob receives a message from alice.
check("A1 bob has one unread sender: alice", len(sb) == 1 and sb[0]["sender"] == "alice")
check("A2 returns alice's owner_email", sb[0]["owner_email"] == "alice@example.com")
check(
    "A3 owner_email_verified is False (self-reported)", sb[0]["owner_email_verified"] is False
)

sc = tabus.bus_unread_senders(con, "carol")  # carol receives a message from bob, who has no email.
check("A5 sender without email returns owner_email None", sc[0]["owner_email"] is None)

# Migration is idempotent when the column already exists.
tabus.migrate(con)
check("A6 migration is safe to repeat with the column present", tabus._nodes_has_owner_email(con))
con.close()

# Part B: reproduce an old database before migration; handle missing columns gracefully, then add them.
# tabus.connect() captures its database path at import and would reuse Part A's database.
# Connect directly with sqlite3 to a fresh file; bus_unread_senders and migrate take only con.
print("Part B: old database without the column (graceful handling)")
import sqlite3  # noqa: E402

con = sqlite3.connect(os.path.join(tempfile.mkdtemp(prefix="email_b_"), "t.db"))
con.row_factory = sqlite3.Row
con.executescript("\n    CREATE TABLE nodes (node_id TEXT PRIMARY KEY, kind TEXT);\n    -- \U0001f534 messages \ub294 \ud604\uc2e4\uc801 \uce78 \uc720\uc9c0(bus_unread_senders \uac00 \uc790\ub77c\uba70 tac_id\xb7accepted_at \ucc38\uc870).\n    --    \uc774 \ud14c\uc2a4\ud2b8\uc758 '\uc61b DB' \ub300\uc0c1\uc740 nodes.owner_email \ub204\ub77d(\uc704)\uc774\uc9c0 messages \ucd5c\uc18c\ud654\uac00 \uc544\ub2c8\ub2e4.\n    CREATE TABLE messages (\n        id TEXT PRIMARY KEY, sender_id TEXT NOT NULL,\n        accepted_at TEXT, tac_id TEXT\n    );\n    CREATE TABLE deliveries (\n        id INTEGER PRIMARY KEY AUTOINCREMENT,\n        message_id TEXT NOT NULL,\n        recipient_id TEXT NOT NULL,\n        state TEXT NOT NULL CHECK(state IN\n            ('ACCEPTED','CLAIMED','INJECTED','READ','PROCESSED','QUARANTINED')),\n        created_at TEXT\n    );\n    ")
st = tabus.UNREAD_STATES[0]
con.execute("INSERT INTO nodes(node_id, kind) VALUES('x','generic'),('y','generic')")
con.execute("INSERT INTO messages(id, sender_id) VALUES('m1','x')")
con.execute(
    "INSERT INTO deliveries(message_id, recipient_id, state, created_at) "
    "VALUES('m1','y',?, '2026-08-28T20:00:00')",
    (st,),
)
con.commit()

check("B0 column is absent", not tabus._nodes_has_owner_email(con))
# Missing columns return email=None without crashing the notifier during deployment skew.
sy = tabus.bus_unread_senders(con, "y")
check(
    "B1 missing column does not crash and still returns the sender",
    len(sy) == 1 and sy[0]["sender"] == "x",
)
check("B2 missing column returns owner_email None", sy[0]["owner_email"] is None)
check("B3 missing column returns owner_email_verified False", sy[0]["owner_email_verified"] is False)

# Migration adds the nodes column before the early return, even if QUARANTINED already exists.
tabus.migrate(con)
check("B4 migration adds owner_email", tabus._nodes_has_owner_email(con))
con.execute("UPDATE nodes SET owner_email='bob@example.com' WHERE node_id='x'")
con.commit()
sy2 = tabus.bus_unread_senders(con, "y")
check("B5 migration followed by registration returns the email", sy2[0]["owner_email"] == "bob@example.com")

# Repeated migration is idempotent, without duplicate ALTER errors.
tabus.migrate(con)
check("B6 second migration is harmless", tabus._nodes_has_owner_email(con))
con.close()

print(f"\n{'❌ FAIL' if fails else '✅ ALL PASS'} — {len(fails)} failed")
sys.exit(1 if fails else 0)
