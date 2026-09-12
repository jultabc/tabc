#!/usr/bin/env python3
"""Register alone bootstraps keys and routes without changing terminal titles."""

import contextlib
import io
import os
import sys
import tempfile
import types

home = tempfile.mkdtemp(prefix="tabc_name_")
os.environ["TABC_HOME"] = home
os.environ.pop("TABC_NODE", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tabus import cli as tabc  # noqa: E402
from tabus import nodekey  # noqa: E402

fails = []


def check(name, condition):
    print(f"  {'PASS' if condition else 'FAIL'}  {name}")
    if not condition:
        fails.append(name)


legacy_node_file = os.path.join(home, "node")
with open(legacy_node_file, "w", encoding="utf-8") as saved:
    saved.write("jack")
tabc._capture_route = lambda: (None, None)
tabc._user_email = lambda: "owner@example.com"
tabc._host_id = lambda: "HOST"
tabc._nodekey = lambda: types.SimpleNamespace(
    key_path=lambda node: os.path.join(home, f".node_key.{node}"),
    public_key_b58=nodekey.public_key_b58,
)

captured = {}


def fake_call(method, path, payload, node=None):
    captured.update(
        {"method": method, "path": path, "payload": payload, "acting": node}
    )
    suffix = (
        f", route={payload['adapter']}, auto_enter="
        f"{'on' if payload.get('auto_enter') else 'off'}"
        if payload.get("adapter")
        else ""
    )
    response = {"ok": True, "msg": f"registered hu{suffix}", "auto_enter_policy": "preserve-same-route"}
    if "auto_enter" in payload:
        response["auto_enter"] = payload.get("auto_enter")
    return 200, response


tabc.call = fake_call
register_output = io.StringIO()
with contextlib.redirect_stdout(register_output):
    tabc.fn_register(types.SimpleNamespace(node="hu", kind="codex"))

with open(legacy_node_file, encoding="utf-8") as saved:
    check("registration does not change the legacy shared node file", saved.read() == "jack")
private_key_file = os.path.join(home, ".node_key.hu")
check("creates a 32-byte node private key", os.path.getsize(private_key_file) == 32)
check("node private key uses mode 0600", os.stat(private_key_file).st_mode & 0o777 == 0o600)
check("initial registration includes the public key", bool(captured["payload"].get("pubkey")))
check("register creates an ordinary node", "program" not in captured["payload"])
check("register preserves kind=codex", captured["payload"].get("kind") == "codex")
check("initial registration is signed with the node's own key", captured["acting"] == "hu")
check("no route without a terminal", "adapter" not in captured["payload"])
check("registration emits no terminal-title control sequences", "\033]" not in register_output.getvalue())

# tmux/iTerm route environment variables may exist even without a TTY; title changes and
# alarm route detection are independent, so registration must still include available routes.
tabc._capture_route = lambda: ("tmux", "/tmp/tmux.sock\t%7")
captured.clear()
route_output = io.StringIO()
with contextlib.redirect_stdout(route_output):
    tabc.fn_register(types.SimpleNamespace(node="hu-route", kind="codex"))
check(
    "non-TTY registration includes an available alarm route",
    captured["payload"].get("adapter") == "tmux"
    and captured["payload"].get("target") == "/tmp/tmux.sock\t%7",
)
check("omitting the registration setting requests preservation", "auto_enter" not in captured["payload"])
check(
    "does not claim no route when an alarm route exists",
    "no terminal doorbell" not in route_output.getvalue(),
)

captured.clear()
with contextlib.redirect_stdout(io.StringIO()):
    tabc.fn_register(types.SimpleNamespace(node="hu-enter", kind="codex", auto_enter="on"))
check("explicit registration on is sent to the server as boolean true", captured["payload"].get("auto_enter") is True)

# A rejected initial registration must not modify the legacy shared node file.
tabc.call = lambda *args, **kwargs: (409, {"error": "signature did not verify"})
try:
    tabc.fn_register(types.SimpleNamespace(node="jiso", kind="codex"))
    check("registration failure exits unsuccessfully", False)
except SystemExit as exc:
    check("registration failure exits unsuccessfully", exc.code == 1)
with open(legacy_node_file, encoding="utf-8") as saved:
    check("failed registration leaves the legacy shared node file unchanged", saved.read() == "jack")

# Reject an empty bootstrap identity with usage error exit 2 before creating a key path.
# An explicitly empty --node must not fall back to TABC_NODE.
os.environ["TABC_NODE"] = "alice"
tabc._nodekey = lambda: (_ for _ in ()).throw(
    AssertionError("empty bootstrap identity reached key loading")
)
for label, fn, ns in (
    ("register minimal", tabc.fn_register, types.SimpleNamespace(node="", kind="codex")),
    (
        "register",
        tabc.fn_register,
        types.SimpleNamespace(node="", kind="codex", program=None),
    ),
):
    output = io.StringIO()
    try:
        with contextlib.redirect_stdout(output):
            fn(ns)
        check(f"empty {label} identity exits 2", False)
    except SystemExit as exc:
        check(
            f"empty {label} identity exits 2 before key operations",
            exc.code == 2 and "empty" in output.getvalue(),
        )
    except AssertionError:
        check(f"empty {label} identity exits 2 before key operations", False)
os.environ.pop("TABC_NODE", None)

# Reject unsafe registration filenames with the same usage error before private key creation.
tabc._nodekey = lambda: nodekey
keys_before_unsafe = {
    name for name in os.listdir(home) if name.startswith(".node_key.")
}
for label, fn, ns in (
    (
        "register minimal",
        tabc.fn_register,
        types.SimpleNamespace(node="../../etc/passwd", kind="codex"),
    ),
    (
        "register",
        tabc.fn_register,
        types.SimpleNamespace(
            node="../../etc/passwd", kind="codex", program=None
        ),
    ),
):
    output = io.StringIO()
    try:
        with contextlib.redirect_stdout(output):
            fn(ns)
        check(f"unsafe {label} identity exits 2", False)
    except SystemExit as exc:
        check(
            f"unsafe {label} identity exits 2 before key creation",
            exc.code == 2 and "invalid node identity" in output.getvalue(),
        )
check(
    "unsafe bootstrap identities do not create private keys",
    {name for name in os.listdir(home) if name.startswith(".node_key.")}
    == keys_before_unsafe,
)

print()
if fails:
    print(f"❌ {len(fails)} FAIL: {fails}")
    sys.exit(1)
print("✅ ALL PASS")
