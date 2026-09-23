#!/usr/bin/env python3
"""Node-key request authentication — the shared bus token is gone; every request is
signed by the acting node's key and verified against its registered public key.

Pins the core: register bootstraps (self-signs against the key it registers), a
signed request then authenticates, an unsigned or wrong-key or stale request is
refused, and a body-tampered request is refused. Temp DB, own HTTP server.
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
TMP = tempfile.mkdtemp(prefix="req_auth_")
os.environ["TABC_DB"] = os.path.join(TMP, "beacon.db")
os.environ["TABC_HOME"] = TMP  # keys + state live here, isolated
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


def _signed(port, method, path, node, keypath, payload=None, ts=None, sig_over=None):
    """Make a request signed by `node`'s key. sig_over overrides the signed bytes
    (to forge a tamper); ts overrides the timestamp (to test the replay window)."""
    body = json.dumps(payload, ensure_ascii=False) if payload is not None else ""
    ts = ts or str(int(time.time()))
    canon = sig_over or nodekey.canonical_request(node, method, path, body, ts)
    sig = nodekey.b58encode(nodekey.sign(canon, keypath))
    headers = {"X-Node": node, "X-Node-Ts": ts, "X-Node-Sig": sig}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    data = body.encode() if payload is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, method=method, headers=headers
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, {}


def _unsigned(port, method, path):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


tabd.init_extras()
alice_key = nodekey.key_path("alice")  # generates under TABC_HOME
alice_pub = nodekey.public_key_b58(alice_key)
mallory_key = nodekey.key_path("mallory")

server = QuietServer(("127.0.0.1", 0), tabd.BusHandler)
threading.Thread(target=server.serve_forever, daemon=True).start()
try:
    port = server.server_port

    # 🔴 no shared token exists any more — an unsigned request is refused
    check("🔴 unsigned request refused (401)", _unsigned(port, "GET", "/who") == 401)

    # register bootstraps: alice self-signs against the key she registers
    code, _ = _signed(port, "POST", "/register", "alice", alice_key,
                      payload={"node": "alice", "kind": "generic", "pubkey": alice_pub})
    check("register bootstraps with a self-signed key", code == 200, f"code={code}")

    # a signed read now authenticates
    code, r = _signed(port, "GET", "/who", "alice", alice_key)
    check("signed request authenticates (who 200)", code == 200)

    # 🔴 wrong key (mallory signs as alice) is refused — sig will not match alice's pubkey
    code, _ = _signed(port, "GET", "/who", "alice", mallory_key)
    check("🔴 wrong key signing as alice refused (401)", code == 401)

    # 🔴 stale timestamp refused (replay window)
    old = str(int(time.time()) - tabd.REQUEST_AUTH_WINDOW_SEC - 60)
    code, _ = _signed(port, "GET", "/who", "alice", alice_key, ts=old)
    check("🔴 stale timestamp refused (401)", code == 401)

    # 🔴 tampered path: signature over a different path than the one requested
    forged = nodekey.canonical_request("alice", "GET", "/who?node=x", "", str(int(time.time())))
    code, _ = _signed(port, "GET", "/who", "alice", alice_key, sig_over=forged)
    check("🔴 path-tampered request refused (401)", code == 401)
    # Search uses the authenticated node, never a caller-supplied query identity.
    from urllib.parse import urlencode
    con = tabus.connect()
    tabus.bus_register(con, "bob", "codex")
    tabus.bus_tac_create(con, "private", '\ud55c\uae00 & 100%', by="alice")
    private = [row["tac_id"] for row in tabus.bus_tac_list(con)
               if row.get("name") == "private"][0]
    tabus.bus_tac_add(con, private, "bob", by="alice")
    con.commit()
    before = list(con.iterdump())
    path = "/tac_search?" + urlencode({"q": '\ud55c\uae00 & 100%', "node": "bob"})
    check("unsigned search refused", _unsigned(port, "GET", path) == 401)
    code, result = _signed(port, "GET", path, "alice", alice_key)
    check("search cannot impersonate membership through query", code == 200 and result["hits"] == [])
    check("search leaves the DB unchanged", list(con.iterdump()) == before)
    tabus.bus_tac_add(con, private, "alice", by="alice")
    con.commit()
    before = list(con.iterdump())
    code, result = _signed(port, "GET", path, "alice", alice_key)
    check("signed search decodes Unicode and punctuation", code == 200 and len(result["hits"]) == 1)
    check("successful search also leaves DB unchanged", list(con.iterdump()) == before)
    code, _ = _signed(port, "GET", "/tac_search?q=x&limit=-1", "alice", alice_key)
    check("search refuses unbounded limit", code == 400)
    code, _ = _signed(port, "GET", "/tac_search?q=x&q=y", "alice", alice_key)
    check("search refuses ambiguous duplicate query", code == 400)
    con.close()
finally:
    server.shutdown()
    server.server_close()

print(f"\n{'FAIL' if fails else 'ALL PASS'} — {len(fails)} failed")
sys.exit(1 if fails else 0)
