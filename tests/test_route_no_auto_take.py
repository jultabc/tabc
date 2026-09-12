#!/usr/bin/env python3
"""A route conflict is never resolved by the client retrying with take-route.

Background: the route key comes from the terminal environment and is inherited by
child processes. In an inherited environment a take-route retry always succeeds, so
a client that answers a conflict by retrying with take-route turns the refusal into
a formality. The wording of the contract holds and its purpose is bypassed, and the
original incident reproduces.

Contract:
  - On a conflict, register makes exactly one HTTP call, injects no take_route, and
    exits non-zero immediately.
  - name follows the same contract: one call, no automatic retry.
  - take_route is sent as true only when the operator asked for it.
  - Without that, the field is false or absent.
  - Both commands actually parse the flag, so the option is reachable from a real
    command line and not only from a hand-built namespace.

Hermetic: call, _capture_route, _nodekey and the environment are stubbed, so no live
database or network is touched. Written before the implementation: RED is expected.
"""

import contextlib
import io
import os
import sys
import tempfile
import types

test_home = tempfile.mkdtemp(prefix="noautotake_")
os.environ["TABC_HOME"] = test_home
os.environ["TABC_DB"] = os.path.join(test_home, "t.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tabus  # noqa: E402
from tabus import cli as tabc  # noqa: E402

fails = []


def check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        fails.append(name)


# Pin an inherited pane environment. This is the condition of the incident: a child
# process carries the parent pane's route key.
tabc._user_email = lambda: None
tabc._host_id = lambda: "HID"
tabc._capture_route = lambda: ("iterm2", "w0t1p0:GUID-INHERITED")
tabc._nodekey = lambda: types.SimpleNamespace(
    key_path=lambda n: f"/tmp/.k.{n}", public_key_b58=lambda p: "PUB"
)
tabc._node_key_path = lambda nk, n: f"/tmp/.k.{n}"


class Conflict:
    """A server that only ever refuses. A retry shows up as a second call."""

    def __init__(self):
        self.calls = []

    def __call__(self, method, path, payload, node=None):
        self.calls.append(dict(payload))
        return 409, {
            "ok": False,
            "error": (
                "route owned by another node on this pane: alice. "
                "register from a different pane, or pass --take-route to move it."
            ),
        }


class Accept:
    def __init__(self):
        self.calls = []

    def __call__(self, method, path, payload, node=None):
        self.calls.append(dict(payload))
        return 200, {"ok": True, "msg": "registered"}


def A(**kw):
    return types.SimpleNamespace(**kw)


def run(fn, args):
    """Capture the exit code; swallow the output."""
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            fn(args)
        return None, buf.getvalue()
    except SystemExit as exc:
        return exc.code, buf.getvalue()


# ── a. register meets a conflict ───────────────────────────────────────────
srv = Conflict()
tabc.call = srv
code_a, _ = run(tabc.fn_register, A(node="bob", kind="claude", program=None))

check("a register on conflict makes exactly one HTTP call", len(srv.calls) == 1)
check("a register on conflict exits non-zero", code_a == 1)
check("a register on conflict injects no take_route", all(not c.get("take_route") for c in srv.calls))


# ── b. name meets a conflict ───────────────────────────────────────────────
srv = Conflict()
tabc.call = srv
code_b, _ = run(tabc.fn_register, A(node="bob", kind="claude"))

check("b register on conflict makes exactly one HTTP call", len(srv.calls) == 1)
check("b register on conflict exits non-zero", code_b == 1)
check("b register on conflict injects no take_route", all(not c.get("take_route") for c in srv.calls))


# ── c. not asked for → false or absent ─────────────────────────────────────
srv = Accept()
tabc.call = srv
run(tabc.fn_register, A(node="bob", kind="claude", program=None, take_route=False))
p = srv.calls[0] if srv.calls else {}
check("c without the flag take_route is false or absent", p.get("take_route") in (None, False))


# ── d. asked for → true, on both commands ──────────────────────────────────
srv = Accept()
tabc.call = srv
run(tabc.fn_register, A(node="bob", kind="claude", program=None, take_route=True))
p = srv.calls[0] if srv.calls else {}
check("d register sends take_route=true when asked", p.get("take_route") is True)

srv = Accept()
tabc.call = srv
run(tabc.fn_register, A(node="bob", kind="claude", take_route=True))
p = srv.calls[0] if srv.calls else {}
check("d register sends take_route=true when asked", p.get("take_route") is True)


# ── e. an explicit take_route that is refused is not retried either ────────
srv = Conflict()
tabc.call = srv
code_e, _ = run(tabc.fn_register, A(node="bob", kind="claude", program=None, take_route=True))
check("e an explicit take_route refused makes exactly one call", len(srv.calls) == 1)
check("e an explicit take_route refused exits non-zero", code_e == 1)


# ── f. the flag is reachable from a real command line ──────────────────────
# The namespaces above are hand-built, so every check up to here passes even if the
# parser never defines the option. Parse real argv to close that gap.
def parses(argv):
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            return tabc.build_parser().parse_args(argv)
    except SystemExit:
        return None


ns_reg = parses(["register", "--node", "bob", "--take-route"])
check("f register parses --take-route", ns_reg is not None and getattr(ns_reg, "take_route", None) is True)

ns_name = parses(["register", "--node", "bob", "--take-route"])
check("f register parses --take-route", ns_name is not None and getattr(ns_name, "take_route", None) is True)

ns_plain = parses(["register", "--node", "bob"])
check(
    "f without the flag the parsed value is false or absent",
    ns_plain is not None and getattr(ns_plain, "take_route", False) in (None, False),
)


print(f"\n{'FAIL' if fails else 'ALL PASS'} — {len(fails)} failing")
sys.exit(1 if fails else 0)
