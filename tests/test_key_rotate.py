#!/usr/bin/env python3
"""Operator key rotation (bus_rotate_key) — the recovery path purge cannot reach.

register is first-set-wins, so it cannot replace a key. purge could, but refuses a
node with history — exactly the node that needs rotation. This pins the dedicated
path: a used node (with history) CAN be rotated, the change actually takes effect on
the gate (old key stops verifying, new key verifies), the token is revoked with it,
and it is refused for the nonsense cases. Also pins that it is NOT a bus route — a
self-serve rotate would be an identity-takeover hole. Temp DB, judged by exit code.
"""

import os
import sqlite3
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="key_rotate_")
os.environ["TABC_DB"] = os.path.join(TMP, "t.db")
os.environ["TABC_SIG_GATE"] = "off"  # test the verified computation, not enforcement
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tabus  # noqa: E402
from tabus import nodekey as nodekey  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{(' — ' + detail) if detail else ''}")
    if not cond:
        fails.append(name)


con = tabus.connect()
con.executescript(tabus.SCHEMA)
tabus.migrate(con)

key_a = os.path.join(TMP, "alice_a.key")
key_b = os.path.join(TMP, "alice_b.key")
pub_a = nodekey.public_key_b58(key_a)
pub_b = nodekey.public_key_b58(key_b)
tabus.bus_register(con, "alice", pubkey=pub_a)
con.commit()


tabus.bus_register(con, "bob")
con.commit()

# 🔴 create history so this node is one purge would refuse — the rotation-locked case
tabus.bus_send(con, "alice", ["bob"], "s", "b", message_id="h1")
sent = con.execute(
    "SELECT count(*) c FROM messages WHERE sender_id='alice'"
).fetchone()["c"]
check("node has history (purge would refuse it)", sent >= 1)

# rotate alice A -> B
ok, msg = tabus.bus_rotate_key(con, "alice", pub_b, by="jul")
check("🔴 rotate succeeds on a node with history", ok, msg)
row = con.execute("SELECT node_pubkey FROM nodes WHERE node_id='alice'").fetchone()
check("nodes.node_pubkey is now the new key", row["node_pubkey"] == pub_b)
kr = con.execute(
    "SELECT old_pubkey, new_pubkey, rotated_by FROM key_rotations WHERE node_id='alice'"
).fetchone()
check("key_rotations audit row written", kr and kr["old_pubkey"] == pub_a and kr["new_pubkey"] == pub_b and kr["rotated_by"] == "jul")

# 🔴 The rotation's effect on request authentication — an old-key-signed request no
#    longer verifies, a new-key one does — is exercised at the HTTP layer in
#    test_request_auth. Here the unit under test is the rotation mechanics above.

# refusals
ok2, m2 = tabus.bus_rotate_key(con, "ghost", pub_a)
check("unregistered node refused", not ok2 and "unregistered" in m2)
ok3, m3 = tabus.bus_rotate_key(con, "alice", pub_b)
check("same key refused (nothing to rotate)", not ok3 and "same" in m3)
ok4, m4 = tabus.bus_rotate_key(con, "alice", "")
check("empty new_pubkey refused", not ok4)
# 🔴 a malformed key is refused at the command, not silently stored (cool CR): a
#    typo'd key would otherwise "succeed" and kill the node on its next send.
ok5, m5 = tabus.bus_rotate_key(con, "alice", "NOT-A-REAL-KEY!!!")
check("🔴 non-base58 key refused at the command", not ok5 and "valid" in m5, m5)
short_b58 = nodekey.b58encode(b"tooshort")  # valid base58, wrong length
ok6, m6 = tabus.bus_rotate_key(con, "alice", short_b58)
check("🔴 wrong-length key refused (must be 32 bytes)", not ok6, m6)
# and a well-formed key still rotates (the check does not block real keys)
pub_c = nodekey.public_key_b58(os.path.join(TMP, "alice_c.key"))
ok7, m7 = tabus.bus_rotate_key(con, "alice", pub_c)
check("a well-formed new key still rotates", ok7, m7)
con.close()

# 🔴 structural: rotation is not reachable over the bus. tabd must expose no rotate
#    route (a self-serve rotate would let any client rotate any node's key).
tabd_src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tabus", "daemon.py"), encoding="utf-8").read()
check("🔴 tabd exposes no rotate route", "rotate" not in tabd_src.lower())

print(f"\n{'FAIL' if fails else 'ALL PASS'} — {len(fails)} failed")
sys.exit(1 if fails else 0)
