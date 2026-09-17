#!/usr/bin/env python3
"""Self-scope — a node may act only on its own node.

Every request is signed, but authentication is not authorization: the acting node
(X-Node, whose signature the daemon verified) must also match the node being acted
on. Reading a mailbox, pulling it, registering, and sending as a sender are all
self-scoped. The supervision reads (who, tacs, tac_messages) are the exceptions.

Pins: a node acts on its own mailbox and sends as itself (200); acting on another
node's mailbox, or sending as another, is refused (403) even with a valid signature;
/who is allowed for any registered node. Temp DB, own HTTP server.
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
TMP = tempfile.mkdtemp(prefix="self_scope_")
os.environ["TABC_DB"] = os.path.join(TMP, "beacon.db")
os.environ["TABC_HOME"] = TMP  # keys + state isolated, off real ~/.tabc
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


def _signed(port, method, path, node, payload=None, response_body=False):
    """A request signed by `node`'s own key. Returns the status code."""
    body = json.dumps(payload, ensure_ascii=False) if payload is not None else ""
    ts = str(int(time.time()))
    canon = nodekey.canonical_request(node, method, path, body, ts)
    sig = nodekey.b58encode(nodekey.sign(canon, nodekey.key_path(node)))
    headers = {"X-Node": node, "X-Node-Ts": ts, "X-Node-Sig": sig}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = body.encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, method=method, headers=headers
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return (r.status, json.load(r)) if response_body else r.status
    except urllib.error.HTTPError as e:
        return e.code


def _register(port, node):
    pub = nodekey.public_key_b58(nodekey.key_path(node))
    return _signed(
        port, "POST", "/register", node, {"node": node, "kind": "generic", "pubkey": pub}
    )


tabd.init_extras()
server = QuietServer(("127.0.0.1", 0), tabd.BusHandler)
threading.Thread(target=server.serve_forever, daemon=True).start()
try:
    port = server.server_port

    check("alice registers herself", _register(port, "alice") == 200)
    check("bob registers himself", _register(port, "bob") == 200)

    # 🔴 register is self-scoped: a node cannot register or re-register another node.
    bob_pub = nodekey.public_key_b58(nodekey.key_path("bob"))
    check(
        "🔴 alice cannot register bob (node != X-Node)",
        _signed(
            port,
            "POST",
            "/register",
            "alice",
            {"node": "bob", "kind": "generic", "pubkey": bob_pub},
        )
        == 403,
    )

    # send: from must equal the acting node — no forging a sender.
    check(
        "alice sends as herself (from=alice)",
        _signed(
            port,
            "POST",
            "/send",
            "alice",
            {"from": "alice", "to": ["bob"], "subject": "s", "body": "b"},
        )
        == 200,
    )
    check(
        "🔴 alice cannot send as bob (from != X-Node)",
        _signed(
            port,
            "POST",
            "/send",
            "alice",
            {"from": "bob", "to": ["alice"], "subject": "s", "body": "b"},
        )
        == 403,
    )

    check("own sent history", _signed(port, "GET", "/sent?node=alice", "alice") == 200)
    check("other sent history refused", _signed(port, "GET", "/sent?node=alice", "bob") == 403)
    check("sent requires node", _signed(port, "GET", "/sent", "alice") == 400)
    for invalid in ("bad", "0", "-1", "201"):
        check("sent limit " + invalid, _signed(port, "GET", "/sent?node=alice&limit=" + invalid, "alice") == 400)
    from tabus.sent import list_sent
    con = tabus.connect()
    before = list(con.execute("SELECT * FROM deliveries"))
    history = list_sent(con, "alice")
    check("sent body and recipient", len(history) == 1 and history[0]["body"] == "b" and history[0]["deliveries"][0]["recipient_id"] == "bob")
    check("recipient has no sent history", list_sent(con, "bob") == [])
    check("other sender id cannot reveal content", list_sent(con, "bob", message_id=history[0]["id"]) == [])
    check("sent read does not change deliveries", before == list(con.execute("SELECT * FROM deliveries")))
    con.close()

    # mailbox: a node reads only its own. bob has one message (alice's send above).
    check("bob reads his own mailbox", _signed(port, "GET", "/mailbox?node=bob", "bob") == 200)
    check(
        "🔴 alice cannot read bob's mailbox",
        _signed(port, "GET", "/mailbox?node=bob", "alice") == 403,
    )
    check("bob pulls his own mailbox", _signed(port, "GET", "/pull?node=bob", "bob") == 200)
    check(
        "🔴 alice cannot pull bob's mailbox",
        _signed(port, "GET", "/pull?node=bob", "alice") == 403,
    )

    # tac contents are member-only, not open supervision: set up a tac with alice
    # in it and bob out.
    _con = tabus.connect()
    tabus.bus_tac_create(_con, "t1", by="alice")
    tabus.bus_tac_add(_con, "t1", "alice", by="alice")
    _con.commit()
    _con.close()
    check("a member reads a tac's contents", _signed(port, "GET", "/tac_messages?tac=t1", "alice") == 200)
    check(
        "🔴 a tac catch-up node must match the signed acting node",
        _signed(
            port,
            "GET",
            "/tac_messages?tac=t1&node=bob",
            "alice",
        )
        == 403,
    )
    check(
        "🔴 a non-member cannot read a tac's contents",
        _signed(port, "GET", "/tac_messages?tac=t1", "bob") == 403,
    )

    # A limited HTTP response must advance exactly its returned delivery IDs.
    con = tabus.connect()
    tabus.bus_tac_create(con, "limited", by="alice")
    for member in ("alice", "bob"):
        tabus.bus_tac_add(con, "limited", member, by="alice")
    limited_ids = [
        tabus.bus_send(con, "alice", [], str(i), "body", tac_id="limited")[0]
        for i in range(3)
    ]
    status, response = _signed(
        port, "GET", "/tac_messages?tac=limited&node=bob&limit=1", "bob",
        response_body=True,
    )
    returned = [m["id"] for m in response.get("messages", [])]
    states = dict(con.execute(
        "SELECT message_id, state FROM deliveries WHERE recipient_id=? "
        "AND message_id IN (SELECT id FROM messages WHERE tac_id=?)",
        ("bob", "limited"),
    ))
    check(
        "HTTP limit advances only the returned ID, never READ",
        status == 200 and returned == [limited_ids[-1]]
        and response.get("marked_read") == 1
        and states == {mid: "INJECTED" if mid in returned else "ACCEPTED" for mid in limited_ids},
    )
    con.close()

    # supervision: /who is allowed for any registered node.
    check("who is allowed for any node (supervision)", _signed(port, "GET", "/who", "alice") == 200)
finally:
    server.shutdown()
    server.server_close()

print(f"\n{'FAIL' if fails else 'ALL PASS'} — {len(fails)} failed")
sys.exit(1 if fails else 0)
