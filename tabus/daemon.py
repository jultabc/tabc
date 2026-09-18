#!/usr/bin/env python3
"""tabd — the tabus bus HTTP server. Standard library only.

The daemon that `tabc` pulls from. The trailing d follows ssh/sshd.
The older name busd survives as a compatibility alias for now: running busd.py
executes this file.

🔴 Security boundary:
- This server pushes nothing into any terminal. It does not even import osascript.
- Clients pull for themselves. That is the first security rule made structural
  rather than procedural.
- Delivery logic is never duplicated here. This file calls the bus_* helpers in
  tabus.py.

Auth: every request is signed by the acting node's key (X-Node / X-Node-Ts /
X-Node-Sig) and verified against that node's registered public key. There is no
shared secret — see _authorized.
"""

import json
import os
import socket
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import nodekey  # base58 signature decoding, for verifying /send
from .send_refusal import declared_length, request_length_refusal
from . import bus as tabus  # the store; kept under its old name so call sites read the same

HOLD_SEC = 25  # longest a long-poll waits
POLL_TICK = 0.5  # how often a waiting long-poll re-checks
# 🔴 How far a request's timestamp may be from the server's clock. This is the
#    lifetime of a captured request: sign it, and it is replayable only within this
#    window. Kept small; a localhost/LAN bus has near-zero clock skew.
REQUEST_AUTH_WINDOW_SEC = 300
# 🔴 Test candidates, not confirmed values.
#    A request over MAX_REQUEST_BYTES is still refused. Its body is read and discarded up
#    to DRAIN_MAX_BYTES within DRAIN_SEC first, so a client still sending it is reading
#    when the 413 arrives. Over DRAIN_MAX_BYTES, or past DRAIN_SEC, the client may get a
#    reset instead.
DRAIN_MAX_BYTES = 16 * 1024 * 1024
DRAIN_SEC = 5
# Longest one socket read may wait for data. A long-poll hold is not a read.
SOCKET_IDLE_SEC = 3
# 🔴 Test candidate, not a confirmed value. The whole body of an accepted request must arrive
#    within this, on a monotonic clock. SOCKET_IDLE_SEC bounds one read; this bounds the
#    request, so a sender that keeps trickling bytes no longer holds a thread indefinitely.
#    It is not applied to a long-poll hold or to writing a response.
BODY_READ_SEC = 30


def stamp_mailbox_open(con, node: str) -> None:
    """Record when a mailbox was opened. Feeds a derived display value."""
    con.execute(
        """INSERT INTO mailbox_opens(node, opened_at)
           VALUES (?, strftime('%Y-%m-%dT%H:%M:%S','now','localtime'))
           ON CONFLICT(node) DO UPDATE SET opened_at=excluded.opened_at""",
        (node,),
    )
    con.commit()


def wire_message(m: dict) -> dict:
    """One shape for every response row. Every endpoint emits through here.

    This exists because sender and from were once both present as keys on the
    same row and drifted apart. One definition prevents that returning.
    """
    out = dict(m)
    out.setdefault("sender", out.get("sender_id"))
    out.setdefault("from", out.get("sender_id"))
    return out


class BusHandler(BaseHTTPRequestHandler):
    server_version = "tabd/1.0"
    # 🔴 StreamRequestHandler applies this to the socket. A read that waits longer raises,
    #    and the request is dropped: a stalled body no longer holds a thread.
    timeout = SOCKET_IDLE_SEC

    def setup(self) -> None:
        self._db_connections = []
        super().setup()

    def _connect(self):
        """A database connection that finish() closes even if the request raised."""
        con = tabus.connect()
        self._db_connections.append(con)
        return con

    def finish(self) -> None:
        try:
            # Release the database lock before tearing down the response socket.
            for con in self._db_connections:
                con.close()
        finally:
            self._db_connections.clear()
            # Overlapping with the explicit close on the normal path is fine;
            # sqlite close is idempotent, and on the error path it rolls back
            # any write transaction that never committed.
            super().finish()

    def _raw_body(self) -> bytes:
        """The request body bytes, read once and cached. Signature verification and
        JSON parsing both need them, and rfile can be read only once."""
        if getattr(self, "_raw_cache", None) is None:
            self._raw_cache = self._read_declared_body()[0]  # only the whole body reaches here
        return self._raw_cache

    def _read_declared_body(self):
        """(bytes read, declared length, how it ended: "whole", "timeout" or "short").

        🔴 One recv at a time (read1), each waiting at most the idle limit or the time left,
        so the whole body is bounded in time as well as in size. A body that ends early is
        "short": the signature does not catch it, because it is verified over the bytes that
        arrived, so a request declaring more than it sends was stored (jiso, jack M3)."""
        # 🔴 Parsed the same way as the length check; int() on the raw header raised on
        #    more than 4,300 digits, including a long run of zeros that the check accepts as 0.
        declared = declared_length(self.headers.get_all("Content-Length"))[1] or 0
        chunks, left, deadline = [], declared, time.monotonic() + BODY_READ_SEC
        try:
            while left > 0:
                wait = deadline - time.monotonic()
                if wait <= 0:
                    return b"".join(chunks), declared, "timeout"
                self.connection.settimeout(min(self.timeout, wait))
                try:
                    chunk = self.rfile.read1(left)
                except (socket.timeout, BlockingIOError):
                    # 🔴 The shortened wait ran out, which is this limit rather than an idle
                    #    stall. Without this the timeout left the request with no answer at all.
                    #    Only a wait that expired counts: a reset connection is not a slow one.
                    if time.monotonic() >= deadline:
                        return b"".join(chunks), declared, "timeout"
                    raise
                if not chunk:
                    break
                chunks.append(chunk)
                left -= len(chunk)
        finally:
            self.connection.settimeout(self.timeout)
        return b"".join(chunks), declared, "whole" if left == 0 else "short"

    def _refused_body_read(self) -> bool:
        """Read the declared body, and answer 408 when it did not all arrive in time.

        🔴 Runs before _authorized, which is what reads the body today. Nothing is stored:
        the request never reaches bus_send."""
        body, declared, ending = self._read_declared_body()
        # 🔴 A partial body is not the request body. Caching it empty keeps a later reader from
        #    treating what did arrive as the whole thing.
        self._raw_cache = body if ending == "whole" else b""
        if ending == "whole":
            return False
        self.close_connection = True
        if ending == "timeout":
            self._json(
                408,
                {
                    "error": "request body took too long",
                    "code": "REQUEST_TIMEOUT",
                    "message": f"the request body did not arrive within {BODY_READ_SEC} seconds",
                    "details": {"limit": BODY_READ_SEC, "unit": "seconds"},
                    "retry": "as_is",
                },
            )
            return True
        # 🔴 The body ended before Content-Length. This is not a timeout, and the signature does
        #    not catch it, so the request is refused here, before anything is stored.
        self._json(
            400,
            {
                "error": "request body is shorter than Content-Length",
                "code": "REQUEST_INCOMPLETE",
                "message": f"the request body ended after {len(body)} of {declared} declared bytes",
                "details": {"bytes": len(body), "declared": declared, "unit": "bytes"},
                "retry": "as_is",
            },
        )
        return True

    def _refused_length(self) -> bool:
        """Answer and return True when the declared request length is refused.

        🔴 This runs before _authorized, because signature verification reads the whole
        body. Without it a registered node could make the daemon hold any Content-Length
        in memory, and a negative length would block the thread reading until EOF.
        """
        refused = request_length_refusal(self.headers.get_all("Content-Length"))
        if refused is None:
            return False
        status, body = refused
        size = body["details"].get("bytes")  # absent when the length is too long to convert
        if status == 413 and size is not None and size <= DRAIN_MAX_BYTES:
            self._discard_body(size)
        self.close_connection = True
        self._json(status, body)
        return True

    def _discard_body(self, size: int) -> None:
        """Read and drop up to size bytes of the request body, for at most DRAIN_SEC.

        🔴 Each pass is one socket read (read1), with the timeout cut to the time left.
        read(n) would keep reading until n bytes arrive, so a slow sender could hold it
        past DRAIN_SEC."""
        left, deadline = size, time.monotonic() + DRAIN_SEC
        try:
            while left > 0:
                wait = deadline - time.monotonic()
                if wait <= 0:
                    break
                self.connection.settimeout(min(self.timeout, wait))
                chunk = self.rfile.read1(min(65536, left))
                if not chunk:
                    break
                left -= len(chunk)
        except OSError:  # includes the timeout; the refusal is sent either way
            pass
        finally:
            self.connection.settimeout(self.timeout)

    def _authorized(self) -> bool:
        """Authenticate the request by node-key signature. There is no shared token.

        The client signs canonical_request(node, method, path, body, ts) with the
        acting node's key; here it is verified against that node's registered public
        key. ts must be within REQUEST_AUTH_WINDOW_SEC, so a captured request cannot
        be replayed later. The signature is the gate; there is no token to present.

        🔴 register bootstraps: a first-time node has no registered key yet, so it
        self-authenticates against the public key it is registering (first-set-wins
        then pins it); a re-register must sign with the already-registered key. Every
        other endpoint requires a key the ledger already holds."""
        node = self.headers.get("X-Node", "")
        ts = self.headers.get("X-Node-Ts", "")
        sig_b58 = self.headers.get("X-Node-Sig", "")
        if not (node and ts and sig_b58):
            return self._deny("missing X-Node / X-Node-Ts / X-Node-Sig header")
        try:
            if abs(int(ts) - int(time.time())) > REQUEST_AUTH_WINDOW_SEC:
                return self._deny("timestamp outside the replay window (clock skew?)")
        except ValueError:
            return self._deny("non-numeric timestamp")
        con = tabus.connect()
        try:
            row = con.execute(
                "SELECT node_pubkey FROM nodes WHERE node_id=?", (node,)
            ).fetchone()
        finally:
            con.close()
        pub = row["node_pubkey"] if row else None
        if not pub and self.path.partition("?")[0] == "/register":
            # 🔴 Bootstrap: no registered key yet, so verify against the key this
            #    request is registering. first-set-wins pins it on success.
            try:
                pub = (json.loads(self._raw_body() or b"{}") or {}).get("pubkey")
            except ValueError:
                pub = None
        if not pub:
            # 🔴 Split the two cases: an operator registers a node that has no row,
            #    but a node that exists without a key is a different (legacy/migration)
            #    situation with a different fix.
            return self._deny(
                "node is registered but has no key" if row else "node is not registered"
            )
        try:
            sig = nodekey.b58decode(sig_b58)
        except Exception:
            return self._deny("signature is not valid base58")
        canon = nodekey.canonical_request(
            node,
            self.command,
            self.path,
            self._raw_body().decode("utf-8", "replace"),
            ts,
        )
        return nodekey.verify(pub, canon, sig) or self._deny("signature did not verify")

    def _deny(self, reason: str) -> bool:
        """Log why a request was refused, and return False.

        🔴 The response stays a single opaque 'token' error — a client is never told
        which check failed, so an attacker learns nothing. The daemon log, which only
        an operator sees, records the reason. That is what tells a normal agent whose
        clock drifted (refused, and the common case) apart from an actual attack;
        without it the two look identical, and the clock case is the first to bite."""
        print(
            f"[tabd] refused {self.command} {self.path.partition('?')[0]} "
            f"node={self.headers.get('X-Node', '')!r}: {reason}",
            flush=True,
        )
        return False

    def _acting(self) -> str:
        """The node this request acts as: the X-Node whose signature _authorized
        verified. A node may act only on its own node. The exceptions are the
        team-wide supervision reads — who (presence) and tacs (the tac list); a tac's
        contents are member-scoped, not open, so /tac_messages checks membership."""
        return self.headers.get("X-Node", "")

    def _json(self, code: int, obj) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        # 🔴 A socket timeout bounds a whole sendall, not the pause between sends. With the
        #    idle timeout left on, a large response to a slow reader was cut off (678,244 of
        #    11,809,904 bytes at 1 s). Writes keep no time limit, as before the idle timeout.
        self.connection.settimeout(None)
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Robots-Tag", "noindex")
            self.end_headers()
            self.wfile.write(body)
        finally:
            self.connection.settimeout(self.timeout)

    def _body(self) -> dict:
        raw = self._raw_body()  # cached; _authorized already read the stream
        if not raw:
            return {}
        try:
            obj = json.loads(raw.decode("utf-8"))
            return obj if isinstance(obj, dict) else {}
        except ValueError:
            return {}

    def do_GET(self):
        if self._refused_length() or self._refused_body_read():
            return
        path, _, query = self.path.partition("?")
        q = dict(kv.split("=", 1) for kv in query.split("&") if "=" in kv)

        if path == "/who":
            if not self._authorized():
                return self._json(401, {"error": "token"})
            con = self._connect()
            nodes = tabus.bus_who_rows(con)
            pending = {
                p["recipient"]: p["pending"]
                for p in tabus.bus_pending_counts_for_all(con)
            }
            opens = {
                r["node"]: r["opened_at"]
                for r in con.execute(
                    "SELECT node, opened_at FROM mailbox_opens"
                ).fetchall()
            }
            con.close()
            out = []
            for nd in nodes:
                nid = nd["node_id"]
                out.append(
                    {
                        **nd,
                        "pending": pending.get(nid, 0),
                        "mailbox_opened_at": opens.get(nid),
                    }
                )
            return self._json(200, {"nodes": out})

        if path == "/sent":
            if not self._authorized():
                return self._json(401, {"error": "token"})
            node = q.get("node")
            if not node:
                return self._json(400, {"error": "node is required"})
            if node != self._acting():
                return self._json(403, {"error": "a node may read only its own sent history"})
            try:
                limit = int(q.get("limit", 20))
                if not 1 <= limit <= 200:
                    raise ValueError()
            except ValueError:
                return self._json(400, {"error": "limit must be an integer from 1 to 200"})
            from .sent import list_sent

            con = self._connect()
            try:
                messages = list_sent(con, node, limit, q.get("id"))
            finally:
                con.close()
            return self._json(200, {"messages": messages})

        if path == "/mailbox":
            if not self._authorized():
                return self._json(401, {"error": "token"})
            node = q.get("node")
            if not node:
                return self._json(400, {"error": "node is required"})
            if node != self._acting():
                return self._json(403, {"error": "a node may read only its own mailbox"})
            # 🔴 int() outside try raised in the handler: no response for limit=abc or 4,301 digits.
            try:
                limit = min(int(q.get("limit", 50)), 200)
            except ValueError:
                return self._json(400, {"error": "limit must be an integer"})
            con = self._connect()
            stamp_mailbox_open(con, node)
            titles = tabus.list_unread_titles(con, node, limit=limit)
            con.close()
            return self._json(
                200,
                {
                    "unread": [
                        {
                            "did": t["did"],
                            "message_id": t["id"],
                            "state": t["state"],
                            "subject": t["subject"],
                            "from": t["sender_id"],
                            "priority": t["priority"],
                        }
                        for t in titles
                    ]
                },
            )

        if path == "/pull":
            if not self._authorized():
                return self._json(401, {"error": "token"})
            node = q.get("node")
            if not node:
                return self._json(400, {"error": "node is required"})
            if node != self._acting():
                return self._json(403, {"error": "a node may pull only its own mailbox"})
            try:
                limit = min(int(q.get("limit", 10)), 50)
            except ValueError:
                return self._json(400, {"error": "limit must be an integer"})
            deadline = time.time() + HOLD_SEC
            con = self._connect()
            msgs, quarantined = [], []
            while True:
                # 🔴 Only claim_new. bus_list_new_titles does not exist.
                #    The earlier title mode was removed: you hold only what was delivered.
                msgs, quarantined, _lost = tabus.claim_new(
                    con, node, limit=limit, claimed_by=f"bus:{node}"
                )
                if msgs or quarantined or time.time() >= deadline:
                    break
                time.sleep(POLL_TICK)
            stamp_mailbox_open(con, node)
            con.close()
            return self._json(
                200,
                {
                    "messages": [wire_message(m) for m in msgs],
                    "quarantined": quarantined,
                },
            )

        if path == "/tacs":
            if not self._authorized():
                return self._json(401, {"error": "token"})
            con = self._connect()
            groups = tabus.bus_tac_list(con)
            con.close()
            return self._json(200, {"tacs": groups})

        if path == "/tac_search":
            if not self._authorized():
                return self._json(401, {"error": "token"})
            from urllib.parse import parse_qs
            from .tac_search import search

            params = parse_qs(query, keep_blank_values=True)
            if any(len(values) != 1 for values in params.values()):
                return self._json(400, {"error": "duplicate query parameters"})
            params = {key: values[0] for key, values in params.items()}
            con = self._connect()
            try:
                hits = search(con, self._acting(), params.get("q"),
                              params.get("tac"), int(params.get("limit", "50")))
            except (ValueError, TypeError) as exc:
                return self._json(400, {"error": str(exc)})
            finally:
                con.close()
            return self._json(200, {"hits": hits})

        if path == "/tac_messages":
            # 🔴 tac contents, read-only, and member-only: a tac is a conversation you
            #    read by joining it, so only its members see the body. A non-member is
            #    refused even with a valid signature.
            if not self._authorized():
                return self._json(401, {"error": "token"})
            gid = q.get("tac")
            if not gid:
                return self._json(400, {"error": "tac is required"})
            # 🔴 Floor the limit at 1. A negative LIMIT means unlimited in SQLite, which
            #    would defeat the ceiling of 200 and dump the whole table. Non-numeric
            #    input is rejected with 400 rather than quietly falling back to a default.
            try:
                limit = max(1, min(int(q.get("limit", 50)), 200))
            except (TypeError, ValueError):
                return self._json(400, {"error": "limit must be an integer"})
            con = self._connect()
            # 🔴 A missing tac answers 200 with exists:false, not 404.
            #    Using 404 for "no such tac" made a client conclude the endpoint itself
            #    was not deployed yet, because 404 then carried two meanings: no route,
            #    and no tac. The exists field splits them. 404 now means no route only.
            exists = tabus._tac_exists(con, gid)
            # 🔴 Member-only: only a member reads the body. A missing tac still answers
            #    exists:false — it leaks no contents — but an existing tac the caller is
            #    not a member of is refused.
            if exists and self._acting() not in tabus.bus_tac_members(con, gid):
                con.close()
                return self._json(403, {"error": "a node may read only a tac it belongs to"})
            # 🔴 Viewing and catch-up are distinct. A signed member may inspect a tac
            #    without changing delivery state. Only an explicit node query requests
            #    catch-up, and it must name the authenticated acting node.
            catch_up_node = q.get("node")
            if "node" in q and not catch_up_node:
                con.close()
                return self._json(400, {"error": "node is required for tac catch-up"})
            if catch_up_node and catch_up_node != self._acting():
                con.close()
                return self._json(403, {"error": "a node may catch up only itself"})
            messages = tabus.bus_tac_messages(con, gid, limit=limit)
            marked = 0
            if exists and catch_up_node:
                marked = tabus.bus_tac_mark_read(
                    con, catch_up_node, gid, message_ids=[m["id"] for m in messages]
                )
            crow = con.execute(
                "SELECT closed_at, close_summary FROM tacs WHERE tac_id=?", (gid,)
            ).fetchone()
            resp = {
                "tac": gid,
                "exists": exists,
                "marked_read": marked,
                "members": tabus.bus_tac_members(con, gid),
                "messages": messages,
                "links": tabus.bus_tac_links(con, gid),
                "closed_at": crow["closed_at"] if crow else None,
                "close_summary": crow["close_summary"] if crow else None,
            }
            con.close()
            return self._json(200, resp)

        return self._json(404, {"error": "not found"})

    def do_POST(self):
        if self._refused_length() or self._refused_body_read():
            return
        if not self._authorized():
            return self._json(401, {"error": "token"})
        path, _, query = self.path.partition("?")
        data = self._body()
        # 🔴 A node may act only on its own node. The target is `from` on /send and
        #    `node` on the mailbox/identity POSTs; the management POSTs (remove,
        #    restore, tac_*) are group/admin actions and are not self-scoped here.
        if path in ("/register", "/beat", "/snooze", "/reopen", "/ack"):
            if data.get("node", "") != self._acting():
                return self._json(403, {"error": "a node may act only on its own node"})
        elif path == "/send":
            if data.get("from", "") != self._acting():
                return self._json(403, {"error": "a node may send only as itself"})
        if (
            path == "/register"
            and "program" in data
            and type(data["program"]) is not bool
        ):
            return self._json(
                409,
                {
                    "ok": False,
                    "msg": "program must be a JSON boolean (true or false)",
                },
            )
        if (
            path == "/register"
            and "auto_enter" in data
            and type(data["auto_enter"]) is not bool
        ):
            return self._json(
                409,
                {
                    "ok": False,
                    "msg": "auto_enter must be a JSON boolean (true or false)",
                },
            )
        con = self._connect()

        if path == "/register":
            for field in ("take_route", "revoke_route", "route_verified"):
                if field in data and type(data[field]) is not bool:
                    con.close()
                    return self._json(409, {"ok": False, "msg": f"{field} must be a JSON boolean (true or false)"})
            registration_meta = {}
            ok, msg = tabus.bus_register(
                con,
                data.get("node", ""),
                data.get("kind", "generic"),
                data.get("instance_id"),
                data.get("boot_id"),
                data.get("capabilities"),
                data.get("adapter"),  # doorbell route; program nodes deliberately omit it
                data.get("target"),
                data.get(
                    "host_id"
                ),  # which installation owns this route, so other machines answer NOT_MINE
                data.get("email"),  # owner identifier in the style of git config. Not auth.
                data.get("pubkey"),  # this node's public key, the node key. First set wins.
                program=(
                    data.get("program") is True
                ),  # literal JSON true only; program mode may reclaim this node's route
                take_route=(
                    data.get("take_route") is True
                ),  # literal JSON true only; never inferred, coerced, or retried
                auto_enter=(
                    data.get("auto_enter")
                ),  # omission preserves only the same active route; new routes stay off
                revoke_route=data.get("revoke_route") is True,
                revoke_adapter=data.get("revoke_adapter"),
                revoke_target=data.get("revoke_target"),
                revoke_host_id=data.get("revoke_host_id"),
                route_verified=data.get("route_verified") is True,
                result_meta=registration_meta,
            )
            if ok:
                response = {"ok": True, "msg": msg, "auto_enter_policy": "preserve-same-route", **registration_meta}
                # A sentence is not a protocol field. `kind` is caller-controlled,
                # so searching the message for "auto_enter=off" lets an old daemon
                # look compatible when that text happens to appear in the kind.
                # Echo the effective stored state in its own typed field instead.
                # No active route is also definitively OFF: there is nowhere that
                # could receive an automatic Enter.
                if "auto_enter" in data:
                    route = con.execute(
                        "SELECT auto_enter FROM tab_routes "
                        "WHERE node_id=? AND revoked_at IS NULL",
                        (data.get("node", ""),),
                    ).fetchone()
                    response["auto_enter"] = bool(route["auto_enter"]) if route else False
                con.close()
                return self._json(200, response)
            code = 409

        elif path == "/beat":
            ok = tabus.bus_beat(con, data.get("node", ""))
            code, msg = (200, "OK") if ok else (404, "unregistered node")

        # 🔴 /dnd was removed. Quiet is one concept now, and /snooze below owns it.
        elif path == "/snooze":
            # 🔴 Timed quiet: silent for `minutes`, absolutely, broadcast included.
            #    With no minutes it reads the current state instead. Returns directly.
            sn = data.get("node", "")
            mins = data.get("minutes")
            if mins is None:
                cur = tabus.bus_get_snooze(con, sn)
                con.close()
                if cur is None:
                    return self._json(404, {"error": f"unregistered node: {sn}"})
                until, remain = cur
                return self._json(
                    200, {"node": sn, "snooze_until": until, "remain_sec": remain}
                )
            try:
                mins = int(mins)
            except (TypeError, ValueError):
                con.close()
                return self._json(400, {"error": "minutes must be a number"})
            ok, msg = tabus.bus_set_snooze(con, sn, mins)
            if not ok:
                con.close()
                return self._json(400, {"error": msg})
            cur = tabus.bus_get_snooze(con, sn)
            con.close()
            until, remain = cur if cur else (None, 0)
            return self._json(
                200,
                {"node": sn, "snooze_until": until, "remain_sec": remain, "msg": msg},
            )

        elif path == "/send":
            # 🔴 The sender is already authenticated: the request signature covers the
            #    body, and `from` must equal the acting node (checked above). No
            #    message-level signature is needed or accepted.
            mid, info = tabus.bus_send(
                con,
                data.get("from", ""),
                data.get("to", []),
                data.get("subject", ""),
                data.get("body"),
                data.get("priority", "next"),
                data.get("expires_at"),
                data.get("reply_to"),
                data.get("message_id"),
                data.get("thread_id"),
                data.get("tac"),  # sending to a tac fans out to its members in bus_send
                bool(
                    data.get("broadcast")
                ),  # an announcement to every non-program, non-removed node other than the sender; overrides SHADOW mode but not a user's snooze
            )
            if mid is None:
                con.close()
                # 🔴 `error` stays the sentence older clients read. A coded refusal adds
                #    code, message, details and retry next to it; the status stays 400.
                refused = {"error": info}
                if hasattr(info, "fields"):
                    refused.update(info.fields())
                return self._json(400, refused)
            pending = {
                p["recipient"]: p["pending"]
                for p in tabus.bus_pending_counts(con, data.get("from", ""))
            }
            con.close()
            return self._json(200, {"id": mid, "info": info, "pending": pending})

        elif path == "/reopen":
            # 🔴 Accept an id prefix. Mailbox listings show a truncated id, and opening
            #    with exactly what was shown used to fail.
            node = data.get("node", "")
            full_id, rerr = tabus.resolve_recipient_message_id(
                con, node, data.get("message_id", "")
            )
            if rerr:
                con.close()
                return self._json(404, {"error": rerr})
            # 🔴 Unpack three values. reopen_unread returns (out, quarantined, lost).
            #    When it grew the third value this caller was not updated, and
            #    dict(rows[0]) blew up here.
            out, quarantined, _lost = tabus.reopen_unread(con, node, full_id)
            con.close()
            for q in quarantined:
                pass  # quarantined rows are not mixed into the response; the first
                # valid row is what goes out, keeping the existing contract
            if not out:
                return self._json(404, {"error": "no such delivery"})
            r0 = dict(out[0])
            return self._json(
                200,
                {
                    "ok": True,
                    "id": r0["id"],
                    "state": r0["state"],
                    "from": r0["sender_id"],
                    "body": r0["body"],
                },
            )

        elif path == "/ack":
            # 🔴 Accept an id prefix here too, for the same reason as open.
            node = data.get("node", "")
            full_id, rerr = tabus.resolve_recipient_message_id(
                con, node, data.get("message_id", "")
            )
            if rerr:
                con.close()
                return self._json(409, {"ok": False, "msg": rerr})
            ok, msg = tabus.bus_ack(con, node, full_id, data.get("state", ""))
            con.close()
            return self._json(200 if ok else 409, {"ok": ok, "msg": msg})

        elif path == "/remove":
            # Remove a node. Soft removal fully deactivates it; purge deletes outright
            # and is allowed only when the node has no history at all.
            # The `by` field lands in the removals audit: deletion needs a person and a log.
            ok, msg = tabus.bus_remove(
                con,
                data.get("node", ""),
                bool(data.get("purge")),
                data.get("by"),
            )
            con.close()
            return self._json(200 if ok else 409, {"ok": ok, "msg": msg})

        elif path == "/restore":
            # Bring a removed node back. The only path that undoes removal, and it audits.
            ok, msg = tabus.bus_restore(con, data.get("node", ""), data.get("by"))
            con.close()
            return self._json(200 if ok else 409, {"ok": ok, "msg": msg})

        elif path == "/tac_create":
            # Create a tac. Permission lives in one place: tabus.tac_action_allowed.
            ok, msg = tabus.bus_tac_create(
                con, data.get("tac", ""), data.get("label"), data.get("by")
            )
            con.close()
            return self._json(200 if ok else 409, {"ok": ok, "msg": msg})

        elif path == "/tac_add":
            ok, msg = tabus.bus_tac_add(
                con, data.get("tac", ""), data.get("node", ""), data.get("by")
            )
            con.close()
            return self._json(200 if ok else 409, {"ok": ok, "msg": msg})

        elif path == "/tac_remove":
            # Drop a member, not the tac. Removing someone absent is an error, not a
            # silent success.
            ok, msg = tabus.bus_tac_remove_member(
                con, data.get("tac", ""), data.get("node", ""), data.get("by")
            )
            con.close()
            return self._json(200 if ok else 409, {"ok": ok, "msg": msg})

        elif path == "/tac_close":
            # 🔴 Close a tac. Closing is final. Already closed or missing is rejected.
            ok, msg = tabus.bus_tac_close(
                con, data.get("tac", ""), data.get("summary"), data.get("by")
            )
            con.close()
            return self._json(200 if ok else 409, {"ok": ok, "msg": msg})

        elif path == "/tac_link":
            # 🔴 Link tacs: a child continues from a parent. Direction, forks, and merges
            #    all live in the junction table.
            ok, msg = tabus.bus_tac_link(
                con, data.get("child", ""), data.get("parent", ""), data.get("by")
            )
            con.close()
            return self._json(200 if ok else 409, {"ok": ok, "msg": msg})

        else:
            con.close()
            return self._json(404, {"error": "not found"})
        con.close()
        return self._json(code, {"ok": ok, "msg": msg})

    def log_message(self, fmt, *args):  # noqa: N802
        print(f"[tabd] {self.address_string()} {fmt % args}", flush=True)


def init_extras() -> None:
    """Ensure the base ledger schema plus the tables tabd owns (mailbox_opens)."""
    con = tabus.connect()
    con.executescript(tabus.SCHEMA)
    tabus.migrate(con)
    con.execute("""CREATE TABLE IF NOT EXISTS mailbox_opens(
                       node TEXT PRIMARY KEY, opened_at TEXT NOT NULL)""")
    con.commit()
    con.close()


def main():
    """Console entry point; see the note in tabus.cli.main."""
    import argparse

    ap = argparse.ArgumentParser(prog="tabd")
    # 🔴 Both versions come from tabus: __version__ is the release, PROTOCOL_VERSION
    #    is the wire format. This endpoint is how you check what the daemon is running.
    ap.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"tabd (tabus) {tabus.__version__}\nprotocol {tabus.PROTOCOL_VERSION}",
    )
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument(
        "--bind",
        default="127.0.0.1",
        help="LAN address, to accept remote clients. Every request is signed regardless.",
    )
    a = ap.parse_args()
    init_extras()
    print(
        f"[tabd] http://{a.bind}:{a.port} listening. Every request is authenticated by node-key signature.",
        flush=True,
    )
    ThreadingHTTPServer((a.bind, a.port), BusHandler).serve_forever()


if __name__ == "__main__":
    main()
