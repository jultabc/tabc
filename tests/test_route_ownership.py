#!/usr/bin/env python3
"""tab_routes route ownership — bus_register unit tests. Temp DB, no live database.

Background: the route key comes from the terminal environment, and a pane holds one
key. Registering a second node name from a pane that another node already owns made
the existing owner's route disappear without telling that owner. Environment
variables are inherited, so every child process started from that pane is a
candidate, not just a deliberate rename.

Contract:
  - A different node holding the same active (host_id, target) makes register refuse.
  - A refused request changes nothing. The incumbent and the requester keep every
    row byte-for-byte, in tab_routes and in nodes both. A refusal must not become an
    outage for a route that was working.
  - Re-registering the same node, and claiming a free target, stay allowed.
  - Only an explicit take_route reclaims, and the displaced node is named in the reply.
  - last_seen_at alone never reclaims. Nothing refreshes that column, so a threshold
    on it would expire live routes.
  - A registration with no route environment creates no route and is not a refusal.

Written before the implementation: RED here is expected.
"""

import inspect
import os
import sys
import tempfile

# Point at a temp database before importing: DB_PATH is fixed at module load.
_tmp = tempfile.mkdtemp(prefix="routeown_test_")
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
# The deployed path (init_extras) exactly: ux_tab_routes_target_active is created
# in migrate, not in SCHEMA.
tabus.migrate(con)
con.commit()

PANE = "w0t1p0:GUID-SHARED-PANE"
HOST = "host-A"


def route(node):
    """The whole row. Comparing a few columns lets the others change unnoticed."""
    r = con.execute(
        "SELECT node_id, adapter, target, host_id, registered_at, last_seen_at, revoked_at "
        "FROM tab_routes WHERE node_id=?",
        (node,),
    ).fetchone()
    return tuple(r) if r is not None else None


def noderow(node):
    """The nodes row, whole. A refusal placed after the nodes UPSERT would leave
    tab_routes untouched and still have changed state, so tab_routes alone cannot
    prove 'changed nothing'."""
    r = con.execute("SELECT * FROM nodes WHERE node_id=?", (node,)).fetchone()
    return tuple(r) if r is not None else None


def register(node, **kw):
    """Returns a marker instead of raising, so one missing contract does not stop
    the rest of the file from reporting."""
    try:
        return tabus.bus_register(con, node, "generic", **kw)
    except TypeError as e:
        return ("TYPEERROR", str(e))


def supports(param):
    try:
        return param in inspect.signature(tabus.bus_register).parameters
    except (TypeError, ValueError):
        return False


# ── 0. the contract exists in the signature ────────────────────────────────
# Without take_route, test 4 cannot even be expressed. Surface that first.
check("0 bus_register accepts take_route", supports("take_route"))


# ── setup: alice owns the pane ─────────────────────────────────────────────
ok0, _ = register("alice", adapter="iterm2", target=PANE, host_id=HOST)
incumbent_route = route("alice")
incumbent_node = noderow("alice")
check("setup alice registered", ok0 is True)
check("setup alice route active", incumbent_route is not None and incumbent_route[6] is None)


# ── 1. same pane, different name → refused, and nothing changed ────────────
# bob already holds a working route on another pane. A refusal here must not
# take that away.
register("bob", adapter="iterm2", target="w0t9p0:GUID-OTHER-PANE", host_id=HOST)
requester_route = route("bob")
requester_node = noderow("bob")

ok1, msg1 = register("bob", adapter="iterm2", target=PANE, host_id=HOST)

check("1 a conflicting registration is refused (ok=False)", ok1 is False)
check("1 incumbent tab_routes row unchanged", route("alice") == incumbent_route)
check("1 requester tab_routes row unchanged", route("bob") == requester_route)
check("1 incumbent nodes row unchanged", noderow("alice") == incumbent_node)
check("1 requester nodes row unchanged", noderow("bob") == requester_node)
check(
    "1 incumbent route still active (revoked_at IS NULL)",
    (route("alice") or (None,) * 7)[6] is None,
)
check(
    "1 a refusal must not report a takeover",
    not (isinstance(msg1, str) and "took over route from" in msg1),
)

# A name that has never registered must not be created by a refused request.
ok1b, _ = register("carol", adapter="iterm2", target=PANE, host_id=HOST)
check("1 a refused first-time registration is refused", ok1b is False)
check("1 a refused first-time registration creates no nodes row", noderow("carol") is None)
check("1 a refused first-time registration creates no route row", route("carol") is None)


# ── 2. same node re-registering → allowed, keeps its route ─────────────────
before2 = route("alice")
ok2, _ = register("alice", adapter="iterm2", target=PANE, host_id=HOST)
after2 = route("alice")
check("2 the same node may re-register", ok2 is True)
check("2 it keeps its own route active", after2 is not None and after2[6] is None)
check("2 registered_at is preserved", before2 is not None and after2[4] == before2[4])


# ── 3. a free target → claimed ─────────────────────────────────────────────
ok3, _ = register("dave", adapter="iterm2", target="w0t3p0:GUID-FREE", host_id=HOST)
check("3 a free target is claimed", ok3 is True)
check("3 the route row exists", route("dave") is not None)


# ── 4. explicit take_route → allowed, names the displaced node ─────────────
ok4, msg4 = register("bob", adapter="iterm2", target=PANE, host_id=HOST, take_route=True)
check("4 an explicit take_route succeeds", ok4 is True)
check("4 the reply names the displaced node", isinstance(msg4, str) and "alice" in msg4)
check("4 the new owner's route is active", (route("bob") or (None,) * 7)[6] is None)
check(
    "4 the previous owner's route is revoked",
    (route("alice") or (None,) * 7)[6] is not None,
)


# ── 5. a different host holding the same target does not interfere ─────────
# This test builds its own pane and owner. Reusing a shared pane makes the result
# depend on how earlier tests happened to end, and then a pass proves nothing.
PANE5 = "w0t5p0:GUID-PANE5"
register("erin", adapter="iterm2", target=PANE5, host_id="host-A")
erin_before = route("erin")
register("frank", adapter="iterm2", target=PANE5, host_id="host-B")
check("5 another host owns the same target independently", (route("frank") or (None,) * 7)[6] is None)
check("5 the host-A owner is untouched", route("erin") == erin_before)

# An older client sends no host_id. If IS ? became = the NULL path would break here.
register("grace", adapter="iterm2", target="w0t5p1:GUID-NULLHOST", host_id=None)
check("5 a registration without host_id creates no route (existing contract)", route("grace") is None)


# ── 6. no route environment → no route, and not a refusal ──────────────────
ok6, _ = register("heidi")
check("6 a registration with no route environment succeeds", ok6 is True)
check("6 no route row is created (pull fallback)", route("heidi") is None)


# ── 7. a stale last_seen_at alone does not reclaim ─────────────────────────
# Nothing refreshes that column, so age is not evidence of death. If a reclaim
# happens here, a live route can be expired silently.
PANE7 = "w0t7p0:GUID-PANE7"
register("ivan", adapter="iterm2", target=PANE7, host_id=HOST)
con.execute(
    "UPDATE tab_routes SET last_seen_at=? WHERE node_id=?",
    ("2000-01-01T00:00:00Z", "ivan"),
)
con.commit()
stale_before = route("ivan")
check("7 setup ivan owns PANE7 actively", (stale_before or (None,) * 7)[6] is None)

ok7, _ = register("judy", adapter="iterm2", target=PANE7, host_id=HOST)
check("7 a stale incumbent is still refused, not reclaimed", ok7 is False)
check("7 the stale incumbent's row is unchanged", route("ivan") == stale_before)
check("7 the latecomer gets no route", route("judy") is None)


# ── 8. the refusal says what to do about it ────────────────────────────────
# Refusing without saying how to proceed leaves the same incident to repeat.
PANE8 = "w0t8p0:GUID-PANE8"
register("karl", adapter="iterm2", target=PANE8, host_id=HOST)
ok8, msg8 = register("laura", adapter="iterm2", target=PANE8, host_id=HOST)
m8 = msg8 if isinstance(msg8, str) else ""
# Checking only for the owner's name would also pass on the current takeover text
# ("took over route from: karl"), so require the refusal first.
check("8 the conflict is refused (ok=False)", ok8 is False)
check("8 the refusal names the current owner", ok8 is False and "karl" in m8)
check("8 the refusal mentions take-route", "take-route" in m8 or "take_route" in m8)
check("8 the refusal mentions using a different pane", "pane" in m8.lower())


print(f"\n{'FAIL' if fails else 'ALL PASS'} — {len(fails)} failing")
sys.exit(1 if fails else 0)
