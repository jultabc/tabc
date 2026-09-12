#!/usr/bin/env python3
"""Beacon TAC team messaging unit tests.

Contract:
- A TAC is a named node set. Sending with tac_id=X fans out to members: one message row and N delivery rows.
- Q1: reject TAC creation when tac_id conflicts with node_id.
- Q2: exclude the sender from its own TAC fanout.
- Removed nodes cannot be added and are excluded from fanout and member counts.
- An empty TAC after excluding the sender must explicitly reject sending, not pretend success.
- group_messages observes conversations by message.tac_id without changing deliveries.
- tac_action_allowed is the shared guard for create, add, rm, and send: patching it to deny must block every path, proving the guard is effective, not merely present.
Temporary database only, never live data. Run directly and check the exit code."""

import os
import sys
import tempfile

os.environ["TABC_DB"] = os.path.join(tempfile.mkdtemp(prefix="group_"), "t.db")
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


con = tabus.connect()
con.executescript(tabus.SCHEMA)
con.commit()
for n in ["alice", "bob", "carol", "dave"]:
    tabus.bus_register(con, n, "generic")
con.commit()


def deliveries_for(mid):
    return sorted(
        r["recipient_id"]
        for r in con.execute(
            "SELECT recipient_id FROM deliveries WHERE message_id=?", (mid,)
        )
    )


# Verify the guard seam exists and v1 allows operations.
ok, reason = tabus.tac_action_allowed(con, "alice", "team", "create")
check("seam: v1 allows (True, None)", ok is True and reason is None)

# ── create ────────────────────────────────────────────────────────
ok, _ = tabus.bus_tac_create(con, "team", '\ud300 \uc804\uccb4', by="alice")
check("create: TAC creation succeeds", ok)
ok, _ = tabus.bus_tac_create(con, "team", by="alice")
check("create: duplicate TAC is rejected", not ok)
ok, msg = tabus.bus_tac_create(con, "alice", by="alice")
check("create: node name collision is rejected (Q1)", not ok and "collision" in msg)

# ── add ───────────────────────────────────────────────────────────
ok, _ = tabus.bus_tac_add(con, "nope", "bob", by="alice")
check("add: nonexistent TAC is rejected", not ok)
ok, _ = tabus.bus_tac_add(con, "team", "ghost", by="alice")
check("add: unregistered node is rejected", not ok)
for m in ["alice", "bob", "carol"]:
    tabus.bus_tac_add(con, "team", m, by="alice")
check(
    "members: three members, alice/bob/carol",
    tabus.bus_tac_members(con, "team") == ["alice", "bob", "carol"],
)
# Duplicate member addition is harmless due to the primary key.
tabus.bus_tac_add(con, "team", "bob", by="alice")
check("add: duplicate member is harmless (still three members)", len(tabus.bus_tac_members(con, "team")) == 3)

# TAC fanout excludes the sender (Q2).
mid, info = tabus.bus_send(con, "alice", [], '\uc548\ub155', '\ubcf8\ubb381', tac_id="team")
check("send: TAC sending succeeds", mid is not None)
check(
    "send: excludes alice, delivering only to bob and carol (Q2)",
    deliveries_for(mid) == ["bob", "carol"],
)
check(
    "send: info.recipients matches the deliveries",
    isinstance(info, dict) and sorted(info["recipients"]) == ["bob", "carol"],
)
grow = con.execute("SELECT tac_id FROM messages WHERE id=?", (mid,)).fetchone()
check("send: records message.tac_id=team", grow and grow["tac_id"] == "team")

# Removed members are excluded from fanout and counts.
tabus.bus_remove(con, "carol")  # soft
check(
    "removed: member listing excludes carol",
    tabus.bus_tac_members(con, "team") == ["alice", "bob"],
)
gl = {g["tac_id"]: g for g in tabus.bus_tac_list(con)}
check("removed: member_count reflects removal (=2)", gl["team"]["member_count"] == 2)
mid2, _ = tabus.bus_send(con, "alice", [], '\uc7ac', '\ubcf8\ubb382', tac_id="team")
check("removed: fanout excludes carol and reaches only bob", deliveries_for(mid2) == ["bob"])
ok, _ = tabus.bus_tac_add(con, "team", "carol", by="alice")
check("removed: cannot add a removed node as a member", not ok)
tabus.bus_restore(con, "carol")

# Reject sending to an empty TAC.
tabus.bus_tac_create(con, "solo", by="alice")
tabus.bus_tac_add(con, "solo", "alice", by="alice")  # Only the sender remains.
mid3, msg3 = tabus.bus_send(con, "alice", [], "x", "y", tac_id="solo")
check("empty: sender-only TAC rejects sending", mid3 is None and "no valid members" in str(msg3))
mid4, msg4 = tabus.bus_send(con, "alice", [], "x", "y", tac_id="none")
check("empty: nonexistent TAC rejects sending", mid4 is None and "no such tac" in str(msg4))

# group_messages is read-only observation: conversation content without state changes.
before = con.execute(
    "SELECT state FROM deliveries WHERE message_id=?", (mid,)
).fetchall()
msgs = tabus.bus_tac_messages(con, "team")
after = con.execute(
    "SELECT state FROM deliveries WHERE message_id=?", (mid,)
).fetchall()
check("show: conversation includes the body", any(m["body"] == '\ubcf8\ubb381' for m in msgs))
check("show: newest first (second body first)", msgs and msgs[0]["body"] == '\ubcf8\ubb382')
check(
    "show: observation does not change delivery state",
    [r["state"] for r in before] == [r["state"] for r in after],
)

# ── remove member ─────────────────────────────────────────────────
ok, _ = tabus.bus_tac_remove_member(con, "team", "bob", by="alice")
check("rm: member removal succeeds", ok and "bob" not in tabus.bus_tac_members(con, "team"))
ok, _ = tabus.bus_tac_remove_member(con, "team", "bob", by="alice")
check("rm: absent member removal is rejected, not silent success", not ok)

# Patching the shared guard to deny must block all guarded paths.
_orig = tabus.tac_action_allowed
tabus.tac_action_allowed = lambda con, req, gid, act: (False, f"deny:{act}")
try:
    okc, _ = tabus.bus_tac_create(con, "t2", by="alice")
    oka, _ = tabus.bus_tac_add(con, "team", "dave", by="alice")
    okr, _ = tabus.bus_tac_remove_member(con, "team", "alice", by="alice")
    mids, msgs_ = tabus.bus_send(con, "alice", [], "s", "b", tac_id="team")
    okcl, _ = tabus.bus_tac_close(con, "team", by="alice")
    # Use distinct TACs so the seam is the first guard; a self-link (team, team) short-circuits before it.
    # Otherwise the test would not prove the guard is effective. solo was created above.
    okln, _ = tabus.bus_tac_link(con, "team", "solo", by="alice")
    check("seam: deny blocks create", not okc)
    check("seam: deny blocks add", not oka)
    check("seam: deny blocks rm", not okr)
    check("seam: deny blocks group send", mids is None and "deny:send" in str(msgs_))
    check("seam: deny blocks close", not okcl)
    check("seam: deny blocks link", not okln)
finally:
    tabus.tac_action_allowed = _orig

# Enforce the lower limit so negative/zero values cannot bypass the 200-row cap and dump everything (bob CR 2026-08-29).
# SQLite treats negative LIMIT as unlimited. team currently has two messages.
check(
    "limit: negative -1 clamps to 1, preventing an unlimited dump",
    len(tabus.bus_tac_messages(con, "team", limit=-1)) == 1,
)
check("limit: zero also clamps to 1", len(tabus.bus_tac_messages(con, "team", limit=0)) == 1)
check(
    "limit: positive value is preserved (two messages)", len(tabus.bus_tac_messages(con, "team", limit=2)) == 2
)

# Regression: deployment order executescript(SCHEMA) then migrate must work with an existing
# messages table lacking tac_id.
# Restoring idx_messages_tac to SCHEMA would crash executescript here,
# catching an upgrade failure that fresh-database tests missed.
import sqlite3 as _sqlite3  # noqa: E402

_legacy = os.path.join(tempfile.mkdtemp(prefix="legacy_"), "t.db")
_lc = _sqlite3.connect(_legacy)
_lc.row_factory = _sqlite3.Row
_lc.executescript(  # Old messages lacks tac_id; old tacs has prev_tac_id but no closure columns.
    "CREATE TABLE messages (id TEXT PRIMARY KEY, thread_id TEXT, accepted_at TEXT);"
    "CREATE TABLE tacs (tac_id TEXT PRIMARY KEY, label TEXT, created_at TEXT, "
    "created_by TEXT, prev_tac_id TEXT);"
)
_lc.commit()
_crash = None
try:
    _lc.executescript(tabus.SCHEMA)  # Deployment applies SCHEMA first.
    _lc.commit()
    tabus.migrate(_lc)  # Migration then adds tac_id and creates idx_messages_tac.
except Exception as _e:  # noqa: BLE001
    _crash = f"{type(_e).__name__}: {_e}"
_lmcols = (
    {r["name"] for r in _lc.execute("PRAGMA table_info(messages)")}
    if not _crash
    else set()
)
_lidx = (
    {r["name"] for r in _lc.execute("PRAGMA index_list(messages)")}
    if not _crash
    else set()
)
check(
    f"deployment order: existing messages without tac_id does not crash ({_crash})", _crash is None
)
check("deployment order: migration adds tac_id", "tac_id" in _lmcols)
check("deployment order: migration creates idx_messages_tac", "idx_messages_tac" in _lidx)
_ltcols = (
    {r["name"] for r in _lc.execute("PRAGMA table_info(tacs)")} if not _crash else set()
)
_ltabs = (
    {
        r["name"]
        for r in _lc.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if not _crash
    else set()
)
check(
    "deployment order: migration adds closure columns to old tacs",
    {"closed_at", "close_summary", "closed_by"} <= _ltcols,
)
check(
    "deployment order: migration drops old prev_tac_id in favor of a junction table",
    "prev_tac_id" not in _ltcols,
)
check("deployment order: creates tac_links junction table", "tac_links" in _ltabs)
_lc.close()

# Fresh schema contains closure columns and tac_links, without prev_tac_id.
_fcols = {r["name"] for r in con.execute("PRAGMA table_info(tacs)")}
_ftabs = {
    r["name"] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
}
check(
    "schema: fresh tacs has closed_at, close_summary, and closed_by",
    {"closed_at", "close_summary", "closed_by"} <= _fcols,
)
check(
    "schema: fresh tacs lacks prev_tac_id, replaced by a junction table", "prev_tac_id" not in _fcols
)
check("schema: tac_links junction table exists", "tac_links" in _ftabs)

# close/link: closure cannot repeat, closed topics reject sending; test link direction, branching, and merging.
tabus.bus_tac_create(con, "p1", by="alice")
tabus.bus_tac_create(con, "p2", by="alice")
tabus.bus_tac_create(con, "c1", by="alice")
tabus.bus_tac_add(con, "p1", "alice", by="alice")
tabus.bus_tac_add(con, "p1", "bob", by="alice")
okc, _ = tabus.bus_tac_close(con, "p1", '\uc815\ub9ac \uc694\uc57d', by="alice")
check("close: TAC closure succeeds", okc)
check(
    "close: closing an already closed TAC is rejected",
    not tabus.bus_tac_close(con, "p1", by="alice")[0],
)
_mx, _msx = tabus.bus_send(con, "alice", [], "s", "b", tac_id="p1")
check("close: closed TAC rejects sending", _mx is None and "was closed" in str(_msx))
check(
    "link: c1 continues both p1 and p2 (merge)",
    tabus.bus_tac_link(con, "c1", "p1", by="alice")[0]
    and tabus.bus_tac_link(con, "c1", "p2", by="alice")[0],
)
check(
    "link: self-link is rejected", not tabus.bus_tac_link(con, "c1", "c1", by="alice")[0]
)
check(
    "link: nonexistent parent is rejected", not tabus.bus_tac_link(con, "c1", "nope", by="alice")[0]
)
_lk = tabus.bus_tac_links(con, "c1")
check("link: c1 has two parents, p1 and p2 (merge)", sorted(_lk["parents"]) == ["p1", "p2"])
check(
    "link: p1 has child c1 (direction and branching)", tabus.bus_tac_links(con, "p1")["children"] == ["c1"]
)

# Split alarm payloads into personal --to and per-TAC groups; conversations belong in TACs (user rule).
tabus.bus_tac_create(con, "proj", by="alice")
for m in ["alice", "bob", "carol"]:
    tabus.bus_tac_add(con, "proj", m, by="alice")
tabus.bus_send(con, "alice", ["bob"], '\uac1c\uc7781', "x")  # Personal --to bob.
tabus.bus_send(con, "alice", ["bob"], '\uac1c\uc7782', "y")
tabus.bus_send(con, "alice", [], '\uc8fc\uc81c1', "p", tac_id="proj")  # tac → bob,carol
# read-before-send (2026-08-30): carol received alice's first topic message and must catch up before sending or be rejected.
tabus.bus_tac_mark_read(con, "carol", "proj")
tabus.bus_send(
    con, "carol", [], '\uc8fc\uc81c\ub9c9\ud310', "q", tac_id="proj"
)  # TAC reaches alice and bob, excluding carol.
_sp = tabus.pending_split_by_tac_for_recipients(con, ["bob", "carol"])
check("payload groups: bob has two personal --to messages", _sp["bob"]["personal"] == 2)
_bp = {t["tac"]: t for t in _sp["bob"]["tacs"]}
check("payload groups: bob has two messages in TAC proj", _bp.get("proj", {}).get("count") == 2)
check(
    "payload groups: bob's newest proj message is carol's final topic message",
    _bp["proj"]["last_sender"] == "carol" and _bp["proj"]["last_subject"] == '\uc8fc\uc81c\ub9c9\ud310',
)
check(
    "payload groups: carol has zero personal messages (sender excluded from fanout)", _sp["carol"]["personal"] == 0
)
_cp = {t["tac"]: t for t in _sp["carol"]["tacs"]}
check(
    "payload groups: carol has one proj message (own send excluded)",
    _cp.get("proj", {}).get("count") == 1,
)

print()
if fails:
    print(f"❌ {len(fails)} FAIL: {fails}")
    sys.exit(1)
print("✅ ALL PASS")
