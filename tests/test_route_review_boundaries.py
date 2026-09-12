#!/usr/bin/env python3
"""Route-review regressions: real CLI namespaces, strict program mode, and races.

This file closes review gaps that unit tests with hand-built namespaces cannot
see. It stays hermetic: the HTTP server, keys, and SQLite database all live in a
temporary directory.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import time
import types
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="route_review_")
os.environ["TABC_DB"] = os.path.join(TMP, "t.db")
os.environ["TABC_HOME"] = TMP
sys.path.insert(0, ROOT)

import tabus  # noqa: E402
from tabus import cli as tabc  # noqa: E402
from tabus import daemon as tabd  # noqa: E402
from tabus import nodekey  # noqa: E402

fails = []


def check(name, condition):
    print(f"  {'PASS' if condition else 'FAIL'}  {name}")
    if not condition:
        fails.append(name)


def run_cli(args):
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            tabc.fn_register(args)
        return None, out.getvalue()
    except SystemExit as exc:
        return exc.code, out.getvalue()


# ── 1. Real argparse namespace reaches fn_register ─────────────────────────
tabc._user_email = lambda: None
tabc._capture_route = lambda: ("iterm2", "CLI-PANE")
tabc._host_id = lambda: "CLI-HOST"
tabc._nodekey = lambda: types.SimpleNamespace(public_key_b58=lambda _path: "PUB")
tabc._node_key_path = lambda _nk, _node: "/tmp/test-route-review-key"

cli_calls = []


def accept_call(_method, _path, payload, node=None):
    cli_calls.append((dict(payload), node))
    return 200, {
        "ok": True,
        "auto_enter_policy": "preserve-same-route",
        "msg": f"registered, route={payload.get('adapter')}",
        "auto_enter": payload.get("auto_enter"),
        "route_active": payload.get("revoke_route") is True,
        "route_provenance_verified": payload.get("revoke_route") is True,
    }


tabc.call = accept_call

args = tabc.build_parser().parse_args(
    ["register", "--node", "hu", "--kind", "codex", "--take-route"]
)
code, _ = run_cli(args)
check("parsed register --take-route exits normally", code is None)
check("parsed register makes exactly one request", len(cli_calls) == 1)
check(
    "parsed register sends literal take_route true",
    bool(cli_calls) and cli_calls[-1][0].get("take_route") is True,
)

cli_calls.clear()
args = tabc.build_parser().parse_args(
    ["register", "--node", "hu", "--kind", "codex"]
)
code, _ = run_cli(args)
check("parsed plain register exits normally", code is None)
check(
    "parsed plain register omits take_route",
    len(cli_calls) == 1 and "take_route" not in cli_calls[0][0],
)
check(
    "a verified CLI route is labelled for legacy-route cleanup safety",
    len(cli_calls) == 1 and cli_calls[0][0].get("route_verified") is True,
)

cli_calls.clear()
tabc._capture_route = lambda: tabc.route_capture.RouteDecision(
    None, None, True, "iterm2", "STALE-CLI-PANE"
)
args = tabc.build_parser().parse_args(
    ["register", "--node", "hu", "--kind", "codex"]
)
code, rejected_output = run_cli(args)
check("a rejected inherited route registers normally", code is None)
check(
    "a rejected inherited route asks for an exact old-route revoke",
    len(cli_calls) == 1
    and cli_calls[0][0].get("revoke_route") is True
    and cli_calls[0][0].get("revoke_adapter") == "iterm2"
    and cli_calls[0][0].get("revoke_target") == "STALE-CLI-PANE"
    and cli_calls[0][0].get("revoke_host_id") == "CLI-HOST",
)
check(
    "a preserved verified route is reported instead of pull-only guidance",
    "existing verified tab route remains active" in rejected_output
    and "Mail arrives by pulling" not in rejected_output,
)
cli_calls.clear()


def accept_unverified_survivor(_method, _path, payload, node=None):
    cli_calls.append((dict(payload), node))
    return 200, {
        "ok": True,
        "msg": "registered hu (kept existing unverified tab route)",
        "auto_enter_policy": "preserve-same-route",
        "route_active": True,
        "route_provenance_verified": False,
    }


tabc.call = accept_unverified_survivor
code, rejected_output = run_cli(args)
check("a rejected inherited route still registers with an unverified survivor", code is None)
check(
    "a surviving unverified route is not reported as pull-only",
    "existing unverified tab route remains active" in rejected_output
    and "Mail arrives by pulling" not in rejected_output,
)
tabc.call = accept_call
cli_calls.clear()


def accept_forged_prose(_method, _path, payload, node=None):
    cli_calls.append((dict(payload), node))
    return 200, {
        "ok": True,
        "msg": "registered hu (codex (kept existing verified tab route))",
        "auto_enter_policy": "preserve-same-route",
        "route_active": False,
    }


tabc._capture_route = lambda: tabc.route_capture.RouteDecision(None, None, False)
tabc.call = accept_forged_prose
plain_args = tabc.build_parser().parse_args(
    ["register", "--node", "hu", "--kind", "codex"]
)
code, forged_output = run_cli(plain_args)
check("server prose cannot forge the structured active-route result", code is None)
check(
    "a prose-only route marker still prints pull guidance",
    "Use read to check messages" in forged_output
    and "existing verified tab route remains active" not in forged_output,
)
tabc.call = accept_call
cli_calls.clear()
tabc._capture_route = lambda: tabc.route_capture.RouteDecision(
    None, None, True, "iterm2", "STALE-CLI-PANE"
)
args = tabc.build_parser().parse_args(
    ["register", "--node", "hu", "--kind", "codex", "--take-route"]
)
code, rejected_takeover_output = run_cli(args)
check("rejected provenance still registers when take-route was requested", code is None)
check(
    "rejected provenance prioritizes revocation over an impossible takeover",
    len(cli_calls) == 1
    and cli_calls[0][0].get("revoke_route") is True
    and "take_route" not in cli_calls[0][0],
)
check(
    "an impossible requested takeover is reported instead of silently dropped",
    "takeover was not attempted" in rejected_takeover_output,
)
tabc._capture_route = lambda: ("iterm2", "CLI-PANE")

cli_calls.clear()
args = tabc.build_parser().parse_args(
    ["register", "--node", "sample-program", "--kind", "codex", "--program"]
)
code, _ = run_cli(args)
program_payload = cli_calls[0][0] if cli_calls else {}
check("parsed --program exits normally", code is None)
check("parsed --program sends literal program true", program_payload.get("program") is True)
check(
    "parsed --program omits every terminal-route field",
    all(field not in program_payload for field in ("adapter", "target", "host_id")),
)

cli_calls.clear()
args = tabc.build_parser().parse_args(
    [
        "register",
        "--node",
        "sample-program",
        "--kind",
        "codex",
        "--program",
        "--take-route",
    ]
)
code, _ = run_cli(args)
check("parsed --program plus --take-route exits 2", code == 2)
check("the rejected CLI combination makes no request", cli_calls == [])


# ── 2. HTTP accepts program mode only from literal JSON true ───────────────
class QuietServer(tabd.ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass


def post(port, node, key_path, payload):
    body = json.dumps(payload, ensure_ascii=False)
    timestamp = str(int(time.time()))
    canonical = nodekey.canonical_request(node, "POST", "/register", body, timestamp)
    signature = nodekey.b58encode(nodekey.sign(canonical, key_path))
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/register",
        data=body.encode(),
        method="POST",
        headers={
            "X-Node": node,
            "X-Node-Ts": timestamp,
            "X-Node-Sig": signature,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def node_state(node):
    con = tabus.connect()
    try:
        row = con.execute(
            "SELECT n.is_program, r.target, r.revoked_at "
            "FROM nodes n LEFT JOIN tab_routes r ON r.node_id=n.node_id "
            "WHERE n.node_id=?",
            (node,),
        ).fetchone()
        return tuple(row) if row else None
    finally:
        con.close()


tabd.init_extras()
server = QuietServer(("127.0.0.1", 0), tabd.BusHandler)
threading.Thread(target=server.serve_forever, daemon=True).start()
try:
    port = server.server_port
    for index, (label, value) in enumerate([
        ("string false", "false"),
        ("string true", "true"),
        ("number 1", 1),
        ("string 1", "1"),
        ("null", None),
    ]):
        node = f"malformed-program-{index}"
        key = nodekey.key_path(node)
        pub = nodekey.public_key_b58(key)
        code, body = post(
            port,
            node,
            key,
            {
                "node": node,
                "kind": "codex",
                "pubkey": pub,
                "program": value,
                "adapter": "iterm2",
                "target": f"MALFORMED-PROGRAM-PANE-{index}",
                "host_id": "PROGRAM-HOST",
            },
        )
        check(f"program {label} is refused", code == 409)
        check(
            f"program {label} names the JSON boolean rule",
            "boolean" in str(body.get("msg") or body.get("error") or "").lower(),
        )
        check(f"program {label} changes no node or route state", node_state(node) is None)

    false_key = nodekey.key_path("literal-false-program")
    false_pub = nodekey.public_key_b58(false_key)
    code, _ = post(
        port,
        "literal-false-program",
        false_key,
        {
            "node": "literal-false-program",
            "kind": "codex",
            "pubkey": false_pub,
            "program": False,
            "adapter": "iterm2",
            "target": "LITERAL-FALSE-PANE",
            "host_id": "PROGRAM-HOST",
        },
    )
    check("literal program false remains a normal registration", code == 200)
    check(
        "literal program false may claim its requested route",
        node_state("literal-false-program") == (0, "LITERAL-FALSE-PANE", None),
    )

    host_only_key = nodekey.key_path("host-only-program")
    host_only_pub = nodekey.public_key_b58(host_only_key)
    code, _ = post(
        port,
        "host-only-program",
        host_only_key,
        {
            "node": "host-only-program",
            "kind": "engine",
            "pubkey": host_only_pub,
            "program": True,
            "host_id": "PROGRAM-HOST",
        },
    )
    check("program plus host_id alone succeeds", code == 200)
    check("host_id alone does not create a route", node_state("host-only-program") == (1, None, None))
finally:
    server.shutdown()
    server.server_close()


# ── 3. Two fresh connections cannot both claim one free route ──────────────
race_failures = []
for iteration in range(25):
    barrier = threading.Barrier(2)
    results = []
    results_lock = threading.Lock()
    target = f"RACE-PANE-{iteration}"

    def claim(node):
        con = tabus.connect()
        try:
            barrier.wait(timeout=5)
            result = tabus.bus_register(
                con,
                node,
                "codex",
                adapter="iterm2",
                target=target,
                host_id="RACE-HOST",
            )
            with results_lock:
                results.append((node, result, None))
        except Exception as exc:
            with results_lock:
                results.append((node, None, repr(exc)))
        finally:
            con.close()

    nodes = (f"race-a-{iteration}", f"race-b-{iteration}")
    threads = [threading.Thread(target=claim, args=(node,)) for node in nodes]
    for worker in threads:
        worker.start()
    for worker in threads:
        worker.join(timeout=15)

    con = tabus.connect()
    active = con.execute(
        "SELECT count(*) c FROM tab_routes "
        "WHERE host_id=? AND target=? AND revoked_at IS NULL",
        ("RACE-HOST", target),
    ).fetchone()["c"]
    registered = con.execute(
        "SELECT count(*) c FROM nodes WHERE node_id IN (?,?)", nodes
    ).fetchone()["c"]
    con.close()

    successes = [result for _, result, error in results if error is None and result[0] is True]
    refusals = [result for _, result, error in results if error is None and result[0] is False]
    errors = [error for _, _, error in results if error is not None]
    if not (
        len(successes) == 1
        and len(refusals) == 1
        and active == 1
        and registered == 1
        and errors == []
    ):
        race_failures.append((iteration, results, active, registered))

check("25 simultaneous free-route claims preserve one owner", race_failures == [])

print(f"\n{'FAIL' if fails else 'ALL PASS'} — {len(fails)} failing")
sys.exit(1 if fails else 0)
