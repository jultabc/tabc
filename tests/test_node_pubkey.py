#!/usr/bin/env python3
"""node_pubkey registration: bus_register stores the public key (first-set-wins), and migrate adds the column.

The initial key survives registration attempts with another key, preventing name takeover. Rotation needs separate user approval.
Migration adds node_pubkey to existing nodes tables, alongside owner_email, to handle code/schema deployment skew.
Uses a temporary database, never live data. Run directly and check the exit code."""

import os
import sqlite3
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="pubkey_")
os.environ["TABC_DB"] = os.path.join(TMP, "t.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tabus  # noqa: E402
from tabus import nodekey as nodekey  # noqa: E402

fails = []


def check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        fails.append(name)


def has_col(con, table, col):
    return any(r["name"] == col for r in con.execute(f"PRAGMA table_info({table})"))


def pubkey_of(con, node):
    r = con.execute("SELECT node_pubkey FROM nodes WHERE node_id=?", (node,)).fetchone()
    return r["node_pubkey"] if r else None


pk1 = nodekey.public_key_b58(os.path.join(TMP, "k1.key"))
pk2 = nodekey.public_key_b58(os.path.join(TMP, "k2.key"))

# Part A: SCHEMA includes the column; registration stores the key with first-set-wins.
print("Part A: registration storage and first-set-wins")
con = tabus.connect()
con.executescript(tabus.SCHEMA)
con.commit()
check("A0 SCHEMA nodes contains node_pubkey", has_col(con, "nodes", "node_pubkey"))
check("A0 the two keys are distinct", pk1 != pk2)

tabus.bus_register(con, "alice", "generic", pubkey=pk1)
con.commit()
check("A1 registration stores the public key", pubkey_of(con, "alice") == pk1)

tabus.bus_register(con, "bob", "generic")
con.commit()
check("A2 registration without a public key stores NULL", pubkey_of(con, "bob") is None)

# Re-registering with another key must retain the original key to prevent name takeover.
tabus.bus_register(con, "alice", "generic", pubkey=pk2)
con.commit()
check("A3 re-registration with a different key retains the original", pubkey_of(con, "alice") == pk1)

tabus.bus_register(con, "alice", "generic")  # Re-register without a key.
con.commit()
check("A4 re-registration without a public key retains the original", pubkey_of(con, "alice") == pk1)

# A node with a NULL key accepts its first key.
tabus.bus_register(con, "bob", "generic", pubkey=pk2)
con.commit()
check("A5 a node with NULL receives its first key", pubkey_of(con, "bob") == pk2)
con.close()

# Part B: migrate adds node_pubkey to an older nodes table without that column.
print("── Part B: migrate ALTER ──")
con = sqlite3.connect(os.path.join(tempfile.mkdtemp(prefix="pubkey_b_"), "t.db"))
con.row_factory = sqlite3.Row
con.executescript("""
    CREATE TABLE nodes (node_id TEXT PRIMARY KEY, kind TEXT);
    CREATE TABLE deliveries (
        did INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT, recipient_id TEXT,
        state TEXT CHECK(state IN ('ACCEPTED','CLAIMED','INJECTED','READ','PROCESSED','QUARANTINED')),
        created_at TEXT
    );
    """)
con.commit()
check("B0 column absent", not has_col(con, "nodes", "node_pubkey"))
tabus.migrate(con)
check("B1 migration adds node_pubkey", has_col(con, "nodes", "node_pubkey"))
tabus.migrate(con)  # Idempotence.
check("B2 migration is idempotent (safe to repeat)", has_col(con, "nodes", "node_pubkey"))
con.close()

print(f"\n{'❌ FAIL' if fails else '✅ ALL PASS'} — {len(fails)} failed")
sys.exit(1 if fails else 0)
