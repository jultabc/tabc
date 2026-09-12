#!/usr/bin/env python3
"""tab_routes push-alarm route registration: bus_register unit tests using a temporary database, never live data.

Contract:
- Registration with both adapter and target creates or updates a tab_routes row.
- Missing either creates no row and falls back to pull; node registration, open, and attach still work.
- Re-registration announces a live terminal, clears revoked_at, and preserves the initial registered_at.
"""

import os
import sqlite3
import sys
import tempfile

# Set the temporary database before importing: DB_PATH is fixed at module load, leaving live tabus.db untouched.
_tmp = tempfile.mkdtemp(prefix="tabroutes_test_")
os.environ["TABC_DB"] = os.path.join(_tmp, "t.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tabus  # noqa: E402

fails = []


def check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        fails.append(name)


con = tabus.connect()
con.executescript(tabus.SCHEMA)
tabus.migrate(
    con
)  # Follow deployment's tabd.init_extras path: migration, not SCHEMA, creates ux_tab_routes_target_active.
con.commit()


def route_of(node):
    return con.execute(
        "SELECT adapter, target, host_id, registered_at, last_seen_at, revoked_at "
        "FROM tab_routes WHERE node_id=?",
        (node,),
    ).fetchone()


# 1. adapter and target, with host_id, create a route row.
ok, msg = tabus.bus_register(
    con, "carol", "generic", adapter="iterm2", target="w0t1p0:GUID-WOO", host_id="h1"
)
r = route_of("carol")
check("1 registration with adapter and target succeeds", ok)
check("1 creates a route row", r is not None)
check("1 adapter=iterm2", bool(r) and r["adapter"] == "iterm2")
check("1 stores target", bool(r) and r["target"] == "w0t1p0:GUID-WOO")
check("1 revoked_at is NULL (active)", bool(r) and r["revoked_at"] is None)
check("1 message reports the route", "route=iterm2" in msg)

# 2. No adapter/target means no route row (pull fallback), but the node is still registered.
ok2, msg2 = tabus.bus_register(con, "bare", "generic")
check("2 route-less registration succeeds", ok2)
check(
    "2 node is registered in nodes",
    con.execute("SELECT 1 FROM nodes WHERE node_id='bare'").fetchone() is not None,
)
check("2 no route row (pull fallback)", route_of("bare") is None)
check("2 message does not report a route", "route=" not in msg2)

# 3. An adapter without a target creates no route; both are required.
tabus.bus_register(con, "half", "generic", adapter="iterm2")
check("3 adapter alone creates no route", route_of("half") is None)

# 4. Exclusive target ownership: only one active route per target; takeover must now be explicit.
# The old contract silently revoked the previous node's route on same-target registration.
# That silent takeover caused real incidents; registration now rejects unless take_route is explicit.
# Reach this state through registration, not manual INSERT.
tabus.bus_register(
    con, "alice", "generic", adapter="iterm2", target="w0:TAB-SHARED", host_id="h1"
)
alice_reg = route_of("alice")["registered_at"]  # Capture the initial registered_at.
check("4 alice's route is active", route_of("alice")["revoked_at"] is None)

# Same-target registration without explicit takeover is rejected; revocation is not the default.
implicit_ok, _ = tabus.bus_register(
    con, "bob", "generic", adapter="iterm2", target="w0:TAB-SHARED", host_id="h1"
)
check("4 same-target registration without explicit takeover is rejected", implicit_ok is False)
check("4 alice's route remains active after rejection", route_of("alice")["revoked_at"] is None)
check("4 bob's route is not created after rejection", route_of("bob") is None)

_, bob_msg = tabus.bus_register(
    con,
    "bob",
    "generic",
    adapter="iterm2",
    target="w0:TAB-SHARED",
    host_id="h1",
    take_route=True,
)
check(
    "4 bob's explicit take_route revokes alice's route",
    route_of("alice")["revoked_at"] is not None,
)
check("4 bob's route is active", route_of("bob")["revoked_at"] is None)
check("4 M2 response names the displaced node", "took over route from: alice" in bob_msg)
check(
    "4 exactly one active route per target (exclusive)",
    con.execute(
        "SELECT COUNT(*) c FROM tab_routes WHERE target='w0:TAB-SHARED' AND revoked_at IS NULL"
    ).fetchone()["c"]
    == 1,
)

# 5. alice can recover from revocation by re-registering at a different target on its own terminal.
tabus.bus_register(
    con, "alice", "generic", adapter="iterm2", target="w0:TAB-ALICE", host_id="h1"
)
ra = route_of("alice")
check("5 alice re-registration reactivates the route (clears revoked_at)", ra["revoked_at"] is None)
check("5 alice's target is updated", ra["target"] == "w0:TAB-ALICE")
check("5 initial registered_at is preserved (excluded from DO UPDATE)", ra["registered_at"] == alice_reg)
check("5 bob is unchanged and remains active", route_of("bob")["revoked_at"] is None)

# 6. Re-registration cannot revoke the node's own route (node_id != self guard).
tabus.bus_register(
    con, "bob", "generic", adapter="iterm2", target="w0:TAB-SHARED", host_id="h1"
)
check(
    "6 bob re-registers the same target and remains active", route_of("bob")["revoked_at"] is None
)

# 7. The partial UNIQUE index enforces M1 exclusivity even for direct INSERT bypassing takeover logic.
# bob owns active w0:TAB-SHARED; register rogue without a route to satisfy the FK, then reject a raw duplicate-target INSERT.
tabus.bus_register(con, "rogue", "generic")  # Register only the node, without a route, to satisfy the FK.
raised, err = False, ""
try:
    con.execute(
        "INSERT INTO tab_routes (node_id, adapter, target, host_id, registered_at, last_seen_at, revoked_at) "
        "VALUES ('rogue','iterm2','w0:TAB-SHARED','h1',?,?,NULL)",  # Same host_id and target as bob.
        (tabus.now_iso(), tabus.now_iso()),
    )
    con.commit()
except sqlite3.IntegrityError as e:
    raised, err = True, str(e)
    con.rollback()
check("7 UNIQUE(host_id,target) rejects duplicate active routes despite bypassed takeover logic", raised)
check("7 rejection is due to UNIQUE, not FK", "UNIQUE" in err or "unique" in err)

# 7b. Application layer requires host_id: adapter and target without host_id create no route and fall back to pull (Jack review).
# Rejection must be visible, distinguishing a terminal missing host_id (B) from no terminal (C), per Jack's 475/476/486 findings.
_, nohost_msg = tabus.bus_register(
    con, "nohost", "generic", adapter="iterm2", target="w0:NOHOST"
)
check(
    "7b route request without host_id creates no route (prevents NULL at source)",
    route_of("nohost") is None,
)
check(
    "7b response reports rejection to prevent silent non-delivery",
    "route refused" in nohost_msg and "no host_id" in nohost_msg,
)
_, bare2_msg = tabus.bus_register(con, "bare2", "generic")  # C: no terminal.
check(
    "7b control: no terminal (C) has no rejection notice; quiet pull fallback is normal",
    'route \uac70\uc808' not in bare2_msg,
)

# 7c. COALESCE(host_id,'') backstop rejects duplicate targets with NULL host_id even through direct SQL (Jack).
tabus.bus_register(con, "nh1", "generic")
con.execute(
    "INSERT INTO tab_routes (node_id, adapter, target, host_id, registered_at, last_seen_at, revoked_at) "
    "VALUES ('nh1','tmux','%NT',NULL,?,?,NULL)",
    (tabus.now_iso(), tabus.now_iso()),
)
con.commit()
tabus.bus_register(con, "nh2", "generic")
raised_c = False
try:
    con.execute(
        "INSERT INTO tab_routes (node_id, adapter, target, host_id, registered_at, last_seen_at, revoked_at) "
        "VALUES ('nh2','tmux','%NT',NULL,?,?,NULL)",
        (tabus.now_iso(), tabus.now_iso()),
    )
    con.commit()
except sqlite3.IntegrityError:
    raised_c = True
    con.rollback()
check("7c COALESCE rejects duplicate targets with NULL host_id", raised_c)

# 8. host_id prevents a different device using the same target, such as tmux %0, from taking the route (Jack 489).
# Targets may overlap across devices; exclusivity applies to the host_id/target pair, avoiding cross-device ownership conflicts.
tabus.bus_register(
    con, "m1node", "generic", adapter="tmux", target="%0", host_id="hostA"
)
tabus.bus_register(
    con, "m2node", "generic", adapter="tmux", target="%0", host_id="hostB"
)
check("8 device A's route remains active, not revoked", route_of("m1node")["revoked_at"] is None)
check("8 device B can register the same target and remain active", route_of("m2node")["revoked_at"] is None)
check(
    "8 host_id is stored",
    route_of("m1node")["host_id"] == "hostA"
    and route_of("m2node")["host_id"] == "hostB",
)
check(
    "8 same target on different devices creates two active rows, one per device",
    con.execute(
        "SELECT COUNT(*) c FROM tab_routes WHERE target='%0' AND revoked_at IS NULL"
    ).fetchone()["c"]
    == 2,
)

# 9. Registration stores the self-reported user email in nodes.owner_email, like git config attribution.
tabus.bus_register(con, "emailnode", "generic", email="owner@example.com")
row9 = con.execute(
    "SELECT owner_email, owner_email_verified FROM nodes WHERE node_id='emailnode'"
).fetchone()
check(
    "9 registration stores email in nodes.owner_email", row9["owner_email"] == "owner@example.com"
)
check(
    "9 self-reported email has owner_email_verified=0 in the schema, not merely a comment",
    row9["owner_email_verified"] == 0,
)
tabus.bus_register(con, "emailnode", "generic")  # Re-register without an email.
oe2 = con.execute("SELECT owner_email FROM nodes WHERE node_id='emailnode'").fetchone()[
    "owner_email"
]
check("9 re-registration without email retains the existing email via COALESCE", oe2 == "owner@example.com")

print(f"\n{'❌ FAIL' if fails else '✅ ALL PASS'} — {len(fails)} failed")
sys.exit(1 if fails else 0)
