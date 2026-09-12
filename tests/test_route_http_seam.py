#!/usr/bin/env python3
"""The route conflict contract survives the HTTP seam. Temp DB, own HTTP server.

Why this file exists: the bus tests and the client tests can both be green while the
daemon between them drops take_route out of the request body. Each side proves only
its own layer, and the seam is where a field goes missing silently. This drives a
real POST /register and judges the status code.

Contract:
  - A conflicting register over HTTP answers 409.
  - The same request with take_route true answers 200 and moves the route.
  - Re-registering the same node still answers 200.
  - A refused request leaves the incumbent's route active.

Written before the implementation: RED is expected.
"""

import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="route_seam_")
os.environ["TABC_DB"] = os.path.join(TMP, "t.db")
os.environ["TABC_HOME"] = TMP  # keys and state stay isolated
sys.path.insert(0, ROOT)

from tabus import daemon as tabd  # noqa: E402
from tabus import nodekey  # noqa: E402
import tabus  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{(' — ' + detail) if detail else ''}")
    if not cond:
        fails.append(name)


class QuietServer(tabd.ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass


def post(port, node, keypath, payload):
    body = json.dumps(payload, ensure_ascii=False)
    ts = str(int(time.time()))
    canon = nodekey.canonical_request(node, "POST", "/register", body, ts)
    sig = nodekey.b58encode(nodekey.sign(canon, keypath))
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/register",
        data=body.encode(),
        method="POST",
        headers={
            "X-Node": node,
            "X-Node-Ts": ts,
            "X-Node-Sig": sig,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        # 🔴 Keep the body. A status code alone cannot say which rule refused, and a
        #    test that only reads 409 passes whenever anything at all goes wrong.
        try:
            return e.code, json.loads(e.read())
        except (ValueError, OSError):
            return e.code, {}


def reason(body):
    return str(body.get("msg") or body.get("error") or "")


PANE = "w0t1p0:GUID-SEAM-PANE"
HOST = "host-A"

tabd.init_extras()
alice_key = nodekey.key_path("alice")
alice_pub = nodekey.public_key_b58(alice_key)
bob_key = nodekey.key_path("bob")
bob_pub = nodekey.public_key_b58(bob_key)


def route_state(node):
    con = tabus.connect()
    try:
        r = con.execute(
            "SELECT target, revoked_at FROM tab_routes WHERE node_id=?", (node,)
        ).fetchone()
        return (r["target"], r["revoked_at"]) if r else None
    finally:
        con.close()


def route_verified(node):
    con = tabus.connect()
    try:
        r = con.execute(
            "SELECT provenance_verified FROM tab_routes WHERE node_id=?", (node,)
        ).fetchone()
        return r["provenance_verified"] if r else None
    finally:
        con.close()


def node_exists(node):
    con = tabus.connect()
    try:
        return con.execute("SELECT 1 FROM nodes WHERE node_id=?", (node,)).fetchone() is not None
    finally:
        con.close()


def route_auto_enter(node):
    con = tabus.connect()
    try:
        row = con.execute(
            "SELECT auto_enter FROM tab_routes WHERE node_id=?", (node,)
        ).fetchone()
        return row["auto_enter"] if row else None
    finally:
        con.close()


server = QuietServer(("127.0.0.1", 0), tabd.BusHandler)
threading.Thread(target=server.serve_forever, daemon=True).start()
try:
    port = server.server_port

    base_alice = {
        "node": "alice",
        "kind": "generic",
        "pubkey": alice_pub,
        "adapter": "iterm2",
        "target": PANE,
        "host_id": HOST,
    }
    code, _ = post(port, "alice", alice_key, base_alice)
    check("setup alice claims the pane over HTTP (200)", code == 200, f"code={code}")
    incumbent = route_state("alice")
    check("setup alice route is active", incumbent == (PANE, None))

    # Re-registering the same node must keep working across the seam.
    code, _ = post(port, "alice", alice_key, base_alice)
    check("the same node re-registering answers 200", code == 200, f"code={code}")
    check("an omitted auto_enter is off over HTTP", route_auto_enter("alice") == 0)

    code, body = post(port, "alice", alice_key, dict(base_alice, auto_enter=True))
    check(
        "literal true enables Enter and returns a typed confirmation over HTTP",
        code == 200
        and route_auto_enter("alice") == 1
        and body.get("auto_enter") is True,
    )
    for label, value in [("string on", "on"), ("number one", 1), ("null", None)]:
        code, body = post(port, "alice", alice_key, dict(base_alice, auto_enter=value))
        check(f"invalid auto_enter {label} is refused", code == 409, reason(body))
        check(f"invalid auto_enter {label} does not change the route", route_auto_enter("alice") == 1)
    code, body = post(port, "alice", alice_key, dict(base_alice, auto_enter=False))
    check(
        "literal false disables Enter and returns a typed confirmation over HTTP",
        code == 200
        and route_auto_enter("alice") == 0
        and body.get("auto_enter") is False,
    )

    no_route_key = nodekey.key_path("auto-no-route")
    no_route_pub = nodekey.public_key_b58(no_route_key)
    code, body = post(
        port,
        "auto-no-route",
        no_route_key,
        {
            "node": "auto-no-route",
            "kind": "codex",
            "pubkey": no_route_pub,
            "auto_enter": True,
        },
    )
    check(
        "automatic Enter without a route is refused over HTTP",
        code == 409 and "complete terminal route" in reason(body),
        f"code={code}",
    )
    check(
        "refused route-less opt-in creates no node over HTTP",
        not node_exists("auto-no-route"),
    )
    check(
        "refused route-less opt-in creates no route over HTTP",
        route_state("auto-no-route") is None,
    )

    route_less_off_key = nodekey.key_path("auto-route-less-off")
    route_less_off_pub = nodekey.public_key_b58(route_less_off_key)
    code, body = post(
        port,
        "auto-route-less-off",
        route_less_off_key,
        {
            "node": "auto-route-less-off",
            "kind": "codex",
            "pubkey": route_less_off_pub,
            "auto_enter": False,
        },
    )
    check(
        "route-less off returns a typed false confirmation over HTTP",
        code == 200
        and body.get("auto_enter") is False
        and type(body.get("auto_enter")) is bool,
        f"code={code}, body={body}",
    )

    base_bob = {
        "node": "bob",
        "kind": "generic",
        "pubkey": bob_pub,
        "adapter": "iterm2",
        "target": PANE,
        "host_id": HOST,
    }

    # No take_route in the body: the conflict must reach the caller as 409.
    code, _ = post(port, "bob", bob_key, dict(base_bob))
    check("a conflicting register without take_route answers 409", code == 409, f"code={code}")
    check("the incumbent route stays active after the refusal", route_state("alice") == (PANE, None))
    check("the refused requester holds no route on that pane", route_state("bob") is None)

    # Only a literal JSON true counts. A permissive read of the field (truthiness on
    # the decoded value) turns the string "false" and the number 1 into consent, so a
    # caller could take a route without ever asking for it — the refusal would be
    # bypassed by the shape of the value rather than by a decision.
    for label, value in [
        ("the string false", "false"),
        ("the string true", "true"),
        ("the number 1", 1),
        ("the string 1", "1"),
    ]:
        code, _ = post(port, "bob", bob_key, dict(base_bob, take_route=value))
        check(f"{label} does not consent — answers 409", code == 409, f"code={code}")
        check(
            f"{label} leaves the incumbent route untouched",
            route_state("alice") == (PANE, None),
        )
        check(f"{label} grants the requester no route", route_state("bob") is None)

    # take_route true must survive the seam and be acted on.
    code, _ = post(port, "bob", bob_key, dict(base_bob, take_route=True))
    check("the same register with take_route answers 200", code == 200, f"code={code}")
    check("the route moved to the requester", route_state("bob") == (PANE, None))
    moved = route_state("alice")
    check("the previous owner's route is revoked", moved is not None and moved[1] is not None)

    # The most privileged combination stays explicit across HTTP: taking an
    # occupied pane does not imply Enter, and Enter does not imply takeover.
    combo_pane = "w0t3p0:GUID-COMBINED-PANE"
    combo_owner_key = nodekey.key_path("combo-owner")
    combo_owner_pub = nodekey.public_key_b58(combo_owner_key)
    code, _ = post(
        port,
        "combo-owner",
        combo_owner_key,
        {
            "node": "combo-owner",
            "kind": "codex",
            "pubkey": combo_owner_pub,
            "adapter": "iterm2",
            "target": combo_pane,
            "host_id": HOST,
        },
    )
    check("setup combined-option pane has an owner", code == 200)
    combo_taker_key = nodekey.key_path("combo-taker")
    combo_taker_pub = nodekey.public_key_b58(combo_taker_key)
    code, _ = post(
        port,
        "combo-taker",
        combo_taker_key,
        {
            "node": "combo-taker",
            "kind": "codex",
            "pubkey": combo_taker_pub,
            "adapter": "iterm2",
            "target": combo_pane,
            "host_id": HOST,
            "take_route": True,
            "auto_enter": True,
        },
    )
    check(
        "explicit takeover plus automatic Enter survives HTTP",
        code == 200
        and route_auto_enter("combo-taker") == 1
        and route_state("combo-owner")[1] is not None,
    )

    # ── the program boundary belongs to the server, not only to the client ──
    # A rule that lives only in the command line becomes the exception as soon as
    # anything speaks HTTP directly. This repository already paid for that once with
    # node-id validation: the signed client path refused unsafe names while a direct
    # POST accepted them.
    #
    # 🔴 These use a pane nobody owns. On an occupied pane a 409 could be the plain
    #    ownership conflict, and the check would pass without ever exercising the
    #    program rule — green, and proving nothing.
    FREE_PANE = "w0t2p0:GUID-UNOWNED-PANE"
    carol_key = nodekey.key_path("carol")
    carol_pub = nodekey.public_key_b58(carol_key)
    base_carol = {"node": "carol", "kind": "generic", "pubkey": carol_pub}
    check("setup the boundary pane is unowned", route_state("carol") is None)

    # 1. a program node that also carries route fields
    code, body = post(
        port,
        "carol",
        carol_key,
        dict(base_carol, program=True, adapter="iterm2", target=FREE_PANE, host_id=HOST),
    )
    check("a program node carrying route fields is refused (409)", code == 409, f"code={code}")
    check("... refused by the program rule, not by an ownership conflict",
          "program nodes cannot" in reason(body), reason(body))
    check("the unowned pane is still unclaimed", route_state("carol") is None)
    check("the refused registration created no node row", not node_exists("carol"))

    # 2. program together with take_route
    code, body = post(
        port,
        "carol",
        carol_key,
        dict(
            base_carol,
            program=True,
            take_route=True,
            adapter="iterm2",
            target=FREE_PANE,
            host_id=HOST,
        ),
    )
    check("program together with take_route is refused (409)", code == 409, f"code={code}")
    check("... refused by the program rule", "program nodes cannot" in reason(body), reason(body))
    check("still no route on the unowned pane", route_state("carol") is None)
    check("still no node row", not node_exists("carol"))

    # 3. take_route asked for while the route fields are incomplete
    dave_key = nodekey.key_path("dave")
    dave_pub = nodekey.public_key_b58(dave_key)
    base_dave = {"node": "dave", "kind": "generic", "pubkey": dave_pub}
    for label, extra in [
        ("no host_id", {"adapter": "iterm2", "target": FREE_PANE}),
        ("no target", {"adapter": "iterm2", "host_id": HOST}),
        ("no adapter", {"target": FREE_PANE, "host_id": HOST}),
        ("no route fields at all", {}),
    ]:
        code, body = post(port, "dave", dave_key, dict(base_dave, take_route=True, **extra))
        check(f"take_route with {label} is refused (409)", code == 409, f"code={code}")
        check(f"take_route with {label} names the incomplete-route rule",
              "complete terminal route" in reason(body), reason(body))
        check(f"take_route with {label} creates no route", route_state("dave") is None)
        check(f"take_route with {label} creates no node row", not node_exists("dave"))

    # 4. a signed agent cannot convert itself to a program or lose its route
    erin_key = nodekey.key_path("erin")
    erin_pub = nodekey.public_key_b58(erin_key)
    ERIN_PANE = "w0t4p0:GUID-ERIN-PANE"
    base_erin = {"node": "erin", "kind": "generic", "pubkey": erin_pub}
    code, _ = post(
        port,
        "erin",
        erin_key,
        dict(base_erin, adapter="iterm2", target=ERIN_PANE, host_id=HOST),
    )
    check("setup erin holds a tab route", code == 200 and route_state("erin") == (ERIN_PANE, None))

    code, _ = post(port, "erin", erin_key, dict(base_erin, program=True))
    check("agent-to-program conversion is refused (409)", code == 409, f"code={code}")
    erin_after = route_state("erin")
    check("refusal preserves its old route", erin_after == (ERIN_PANE, None))
    check("it leaves another node's route alone", route_state("bob") == (PANE, None))

    # 5. A signed client that saw route variables but could not prove terminal
    # ownership can revoke only the exact previously stored route it observed.
    route_less_bob = {
        k: v for k, v in base_bob.items() if k not in ("adapter", "target", "host_id")
    }
    code, body = post(
        port,
        "bob",
        bob_key,
        dict(
            route_less_bob,
            revoke_route=True,
            revoke_adapter="iterm2",
            revoke_target=PANE,
            revoke_host_id=HOST,
        ),
    )
    check("rejected provenance can revoke the matching old route", code == 200)
    bob_after = route_state("bob")
    check("the exact matching old route is revoked", bob_after is not None and bob_after[1] is not None)

    # A route migrated from the schema before host_id has NULL there. The current
    # client can still identify the exact node, adapter, and target, and must be
    # able to retire that unverified legacy row rather than leave a stale bell.
    legacy_key = nodekey.key_path("legacy-null-host")
    legacy_pub = nodekey.public_key_b58(legacy_key)
    legacy_base = {
        "node": "legacy-null-host",
        "kind": "generic",
        "pubkey": legacy_pub,
    }
    LEGACY_PANE = "w0t4p1:GUID-LEGACY-NULL-HOST"
    code, _ = post(
        port,
        "legacy-null-host",
        legacy_key,
        dict(
            legacy_base,
            adapter="iterm2",
            target=LEGACY_PANE,
            host_id=HOST,
        ),
    )
    con = tabus.connect()
    con.execute(
        "UPDATE tab_routes SET host_id=NULL WHERE node_id=?",
        ("legacy-null-host",),
    )
    con.commit()
    con.close()
    code, body = post(
        port,
        "legacy-null-host",
        legacy_key,
        dict(
            legacy_base,
            revoke_route=True,
            revoke_adapter="iterm2",
            revoke_target=LEGACY_PANE,
            revoke_host_id=HOST,
        ),
    )
    legacy_after = route_state("legacy-null-host")
    check("a hostless migrated route cleanup succeeds", code == 200, reason(body))
    check(
        "the exact hostless unverified legacy route is revoked",
        legacy_after is not None and legacy_after[1] is not None,
    )

    # A delayed rejection for route A can arrive after an older client has moved
    # the same node to unverified route B. The exact revoke must preserve B, and
    # the response must say that a route still survives instead of advertising
    # pull-only delivery.
    legacy_move_key = nodekey.key_path("legacy-moved-route")
    legacy_move_pub = nodekey.public_key_b58(legacy_move_key)
    legacy_move_base = {
        "node": "legacy-moved-route",
        "kind": "generic",
        "pubkey": legacy_move_pub,
    }
    LEGACY_MOVE_A = "w0t4p7:GUID-LEGACY-MOVE-A"
    LEGACY_MOVE_B = "w0t4p8:GUID-LEGACY-MOVE-B"
    code, _ = post(
        port,
        "legacy-moved-route",
        legacy_move_key,
        dict(
            legacy_move_base,
            adapter="iterm2",
            target=LEGACY_MOVE_A,
            host_id=HOST,
        ),
    )
    code, _ = post(
        port,
        "legacy-moved-route",
        legacy_move_key,
        dict(
            legacy_move_base,
            adapter="iterm2",
            target=LEGACY_MOVE_B,
            host_id=HOST,
        ),
    )
    code, body = post(
        port,
        "legacy-moved-route",
        legacy_move_key,
        dict(
            legacy_move_base,
            revoke_route=True,
            revoke_adapter="iterm2",
            revoke_target=LEGACY_MOVE_A,
            revoke_host_id=HOST,
        ),
    )
    check("a delayed old-route rejection still registers", code == 200, reason(body))
    check(
        "a delayed rejection preserves the newer unverified route",
        route_state("legacy-moved-route") == (LEGACY_MOVE_B, None),
    )
    check(
        "a surviving unverified route is named in the response",
        "kept existing unverified tab route" in reason(body),
        reason(body),
    )
    check(
        "the response reports the surviving route without parsing prose",
        body.get("route_active") is True
        and body.get("route_provenance_verified") is False,
        str(body),
    )

    # Verification is a permanent floor for this route row, not merely a property
    # of its active interval. After another node takes the pane, an older client
    # must not reactivate and downgrade the revoked verified row elsewhere.
    victim_key = nodekey.key_path("verified-victim")
    victim_pub = nodekey.public_key_b58(victim_key)
    taker_key = nodekey.key_path("verified-taker")
    taker_pub = nodekey.public_key_b58(taker_key)
    VERIFIED_PANE = "w0t4p2:GUID-VERIFIED-VICTIM"
    code, _ = post(
        port,
        "verified-victim",
        victim_key,
        {
            "node": "verified-victim",
            "kind": "generic",
            "pubkey": victim_pub,
            "adapter": "iterm2",
            "target": VERIFIED_PANE,
            "host_id": HOST,
            "route_verified": True,
        },
    )
    code, _ = post(
        port,
        "verified-taker",
        taker_key,
        {
            "node": "verified-taker",
            "kind": "generic",
            "pubkey": taker_pub,
            "adapter": "iterm2",
            "target": VERIFIED_PANE,
            "host_id": HOST,
            "route_verified": True,
            "take_route": True,
        },
    )
    victim_revoked = route_state("verified-victim")
    check(
        "setup takeover revokes but retains the verified route row",
        code == 200
        and victim_revoked is not None
        and victim_revoked[1] is not None
        and route_verified("verified-victim") == 1,
    )
    code, body = post(
        port,
        "verified-victim",
        victim_key,
        {
            "node": "verified-victim",
            "kind": "generic",
            "pubkey": victim_pub,
            "adapter": "iterm2",
            "target": "w0t4p3:GUID-OLD-CLIENT-REACTIVATE",
            "host_id": HOST,
        },
    )
    check("an old client cannot reactivate a revoked verified row", code == 409)
    check(
        "the refusal explains that earlier verification still applies",
        "previously verified terminal route" in reason(body),
        reason(body),
    )
    check(
        "the revoked verified row stays revoked and verified",
        route_state("verified-victim") == victim_revoked
        and route_verified("verified-victim") == 1,
        reason(body),
    )

    # A delayed rejection from pane A must not revoke pane B after the node has
    # already moved. This is the race an unscoped node-only UPDATE permits.
    BOB_PANE_B = "w0t5p0:GUID-BOB-PANE-B"
    bob_b = dict(base_bob, target=BOB_PANE_B, route_verified=True)
    code, _ = post(port, "bob", bob_key, bob_b)
    check("setup bob moves to a new valid pane", code == 200 and route_state("bob") == (BOB_PANE_B, None))
    check("a new client marks its terminal proof on the route", route_verified("bob") == 1)

    # Once a route has exact TTY proof, an older client that cannot send that
    # proof must not move or reactivate it. It may refresh the exact same active
    # identity during a rolling update, but the proof marker stays verified.
    code, body = post(port, "bob", bob_key, dict(base_bob))
    check("an old client cannot replace a verified route", code == 409)
    check("an old-client replacement names the verification rule", "verified terminal route" in reason(body), reason(body))
    check("an old-client replacement leaves pane B active", route_state("bob") == (BOB_PANE_B, None))
    check("an old-client replacement cannot downgrade the proof marker", route_verified("bob") == 1)

    code, body = post(port, "bob", bob_key, dict(bob_b, route_verified=False))
    check("an old client can refresh the same active verified target", code == 200)
    check(
        "a same-target old-client refresh keeps pane B verified",
        route_state("bob") == (BOB_PANE_B, None) and route_verified("bob") == 1,
    )
    code, body = post(
        port,
        "bob",
        bob_key,
        dict(
            route_less_bob,
            revoke_route=True,
            revoke_adapter="iterm2",
            revoke_target=PANE,
            revoke_host_id=HOST,
        ),
    )
    check("a late stale-pane revoke request still registers", code == 200)
    check("a late stale-pane request cannot revoke the new pane", route_state("bob") == (BOB_PANE_B, None))

    # ABA: the node returns to the exact old address. Exact identity alone is not
    # enough; a rejected legacy request must not revoke a newly verified claim.
    bob_a_verified = dict(base_bob, route_verified=True)
    code, _ = post(port, "bob", bob_key, bob_a_verified)
    check("setup bob validly returns to the original pane", code == 200 and route_state("bob") == (PANE, None))
    code, body = post(
        port,
        "bob",
        bob_key,
        dict(
            route_less_bob,
            revoke_route=True,
            revoke_adapter="iterm2",
            revoke_target=PANE,
            revoke_host_id=HOST,
        ),
    )
    check("a stale exact-address revoke still registers", code == 200)
    check("a stale exact-address revoke cannot disable a verified route", route_state("bob") == (PANE, None))
    check(
        "a surviving verified route is named in the response",
        "kept existing verified tab route" in reason(body),
        reason(body),
    )
    check(
        "the response reports verified survival as structured data",
        body.get("route_active") is True
        and body.get("route_provenance_verified") is True,
        str(body),
    )

    code, _ = post(port, "bob", bob_key, bob_b)
    check("setup bob returns to pane B for boundary checks", code == 200 and route_state("bob") == (BOB_PANE_B, None))

    for label, missing in [
        ("adapter", "revoke_adapter"),
        ("target", "revoke_target"),
        ("host", "revoke_host_id"),
    ]:
        revoke = {
            "revoke_route": True,
            "revoke_adapter": "iterm2",
            "revoke_target": BOB_PANE_B,
            "revoke_host_id": HOST,
        }
        del revoke[missing]
        code, body = post(port, "bob", bob_key, dict(route_less_bob, **revoke))
        check(f"revoke without {label} is refused", code == 409)
        check(f"revoke without {label} names exact identity", "complete rejected route" in reason(body), reason(body))
        check(f"revoke without {label} leaves the new pane active", route_state("bob") == (BOB_PANE_B, None))

    code, body = post(
        port,
        "bob",
        bob_key,
        dict(
            route_less_bob,
            revoke_route=True,
            revoke_adapter=["iterm2"],
            revoke_target=BOB_PANE_B,
            revoke_host_id=HOST,
        ),
    )
    check("a non-string rejected route field is refused", code == 409)
    check("a non-string rejected route field names exact identity", "complete rejected route" in reason(body), reason(body))
    check("a malformed revoke leaves the new pane active", route_state("bob") == (BOB_PANE_B, None))

    code, body = post(
        port,
        "bob",
        bob_key,
        dict(
            route_less_bob,
            revoke_route=False,
            revoke_adapter="iterm2",
            revoke_target=BOB_PANE_B,
            revoke_host_id=HOST,
        ),
    )
    check("rejected route fields without consent are refused", code == 409)
    check("rejected fields without consent name the flag rule", "require revoke_route=true" in reason(body), reason(body))
    check("rejected fields without consent leave the new pane active", route_state("bob") == (BOB_PANE_B, None))

    code, body = post(
        port,
        "bob",
        bob_key,
        dict(bob_b, route_verified="true"),
    )
    check("route_verified string true is refused", code == 409)
    check("route_verified string true names the JSON boolean rule", "route_verified must be a JSON boolean" in reason(body), reason(body))
    check("malformed route verification leaves pane B active", route_state("bob") == (BOB_PANE_B, None))

    # No route variables is different from rejected route variables. A plain
    # route-less registration must preserve a valid route claimed earlier.
    code, _ = post(port, "alice", alice_key, dict(base_alice))
    check("setup alice restores a valid route", code == 200 and route_state("alice") == (PANE, None))
    route_less_alice = {
        k: v for k, v in base_alice.items() if k not in ("adapter", "target", "host_id")
    }
    code, _ = post(port, "alice", alice_key, route_less_alice)
    check("plain route-less registration succeeds", code == 200)
    check("plain route-less registration preserves a valid old route", route_state("alice") == (PANE, None))

    for label, value in [("string true", "true"), ("number 1", 1)]:
        code, body = post(
            port,
            "alice",
            alice_key,
            dict(route_less_alice, revoke_route=value),
        )
        check(f"revoke_route {label} is refused", code == 409)
        check(
            f"revoke_route {label} names the JSON boolean rule",
            "revoke_route must be a JSON boolean" in reason(body),
            reason(body),
        )
        check(f"revoke_route {label} leaves the route active", route_state("alice") == (PANE, None))
finally:
    server.shutdown()
    server.server_close()

print(f"\n{'FAIL' if fails else 'ALL PASS'} — {len(fails)} failing")
sys.exit(1 if fails else 0)
