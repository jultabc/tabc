#!/usr/bin/env python3
"""Acceptance tests: automatically verify all Jack's criteria; exit 1 on any failure.

Isolation: run tabd in a separate process with a temporary database, not a shared client
file, and on a port this test owns.

🔴 This file used to hold PORT = 8766, which is a port the bus itself uses. When a bus
was running there, every signed request in this file went to the live ledger instead of
the daemon this test starts (measured 2026-09-18: the requests were refused, so nothing
was written, but they arrived). The port now comes from `server_harness`, which takes a
free one, refuses the two the bus uses, and proves the daemon answering is the one this
test spawned before any request is sent.
"""

import ast
import atexit
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# 🔴 Safe to import here, above the TABC_HOME line below, because this helper imports
#    nothing from the package at import time. `tabus.paths` reads TABC_HOME once, when it
#    is imported, so anything that pulled the package in before that line would freeze the
#    wrong home — and the CLI would then sign with keys from a different directory than
#    the one this file registers from. That happened; the harness now imports lazily.
import server_harness  # noqa: E402  — the port and the ownership check live there

# 🔴 Both are set in main(), once the port is known. Nothing may be requested before
#    that: a module-level BASE is how this file used to point at the bus.
PORT = None
BASE = None

# 🔴 No shared bus token any more: every request is authenticated by the acting
#    node's key signature. Keys live under an isolated home so the suite never
#    touches the real ~/.tabc.
KEYDIR = tempfile.mkdtemp(prefix="acc_keys_")
os.environ["TABC_HOME"] = KEYDIR
sys.path.insert(0, ROOT)
from tabus import nodekey  # noqa: E402

FAILS = []
PASSES = []


def check(name, ok, detail=""):
    (PASSES if ok else FAILS).append((name, detail))
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""))


def call(method, path, payload=None, node=None, expect_error=False):
    """HTTP call signed by the acting node's key — the request-auth layer.

    The acting node is derived from the call when not passed: a POST body names it
    as `from` (send) or `node` (register/reopen/ack), a GET names it in the query as
    node=. Every request in this suite carries exactly one, so the signature always
    binds to the node the request acts as."""
    body = json.dumps(payload, ensure_ascii=False) if payload is not None else ""
    req = urllib.request.Request(BASE + path, method=method)
    data = None
    if payload is not None:
        data = body.encode("utf-8")
        req.add_header("Content-Type", "application/json; charset=utf-8")
    acting = node
    if acting is None and payload:
        acting = payload.get("from") or payload.get("node")
    if acting is None and "node=" in path:
        acting = path.split("node=", 1)[1].split("&", 1)[0]
    if acting:
        ts = str(int(time.time()))
        sig = nodekey.b58encode(
            nodekey.sign(
                nodekey.canonical_request(acting, method, path, body, ts),
                nodekey.key_path(acting),
            )
        )
        req.add_header("X-Node", acting)
        req.add_header("X-Node-Ts", ts)
        req.add_header("X-Node-Sig", sig)
    try:
        with urllib.request.urlopen(req, data=data, timeout=35) as res:
            return res.status, json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8") or "{}"
        try:
            return e.code, json.loads(body)
        except ValueError:
            return e.code, {}


def register(node):
    """Bootstrap-register `node`: generate its key and self-sign the registration with
    the pubkey it registers (first-set-wins pins it)."""
    pub = nodekey.public_key_b58(nodekey.key_path(node))
    return call("POST", "/register", {"node": node, "kind": "test", "pubkey": pub})


def s_ast_single_definitions():
    """Automatically count whether each name is defined only once in the file (Jack)."""
    src = open(os.path.join(ROOT, "tabus", "bus.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    names = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    targets = ["list_unread_titles", "claim_new", "reopen_unread", "bus_ack"]
    dups = {t: names.count(t) for t in targets if names.count(t) != 1}
    check("S0 single definition", not dups, str(dups) if dups else '4 \ud568\uc218 \uac01 1\ud68c')


def main():
    tmpdir = tempfile.mkdtemp(prefix="bus_acc_")
    dbpath = os.path.join(tmpdir, "acc.db")
    spooldir = os.path.join(tmpdir, "doorbell")  # the doorbell file spool, read back in S10/S11
    # 🔴 The daemon starts before BASE exists, and BASE is the port it reported. A
    #    failure here raises, and the test stops without sending a request anywhere.
    global PORT, BASE
    try:
        proc, PORT, BASE, log_path = server_harness.start(
            ROOT, tmpdir, dbpath, {"TABC_DOORBELL_SPOOL": spooldir}
        )
    except server_harness.ServerStartFailed as failure:
        print(f"FAIL server did not start — {failure}")
        print("\n=== Results: PASS 0 / FAIL 1 (no request was sent) ===")
        sys.exit(1)
    # 🔴 Registered before anything else can fail. The explicit stop at the end runs on
    #    the path where every check passes; this one runs on the paths where they do not,
    #    so a failure part way through does not leave a daemon holding a port (hu).
    atexit.register(server_harness.stop, proc)
    # 🔴 Everything below runs inside try/finally: the daemon this test started is
    #    ended even when a check raises, and not only when the process exits.
    #    atexit above is the second net, for an exit that skips this frame.
    try:
        check("S0a the daemon answering is the one this test started", proc.poll() is None,
              f"port={PORT} log={log_path}")
        env = dict(os.environ, TABC_DB=dbpath, TABC_BUS_URL=BASE, TABC_DOORBELL_SPOOL=spooldir)
        env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")

        # S1 registration.
        c1, r = register("n1")
        check("S1 register n1", c1 == 200 and r.get("ok"))
        _, r2 = register("n2")
        check("S1b register n2", r2.get("ok"))

        # S2 sending.
        c2, r3 = call(
            "POST",
            "/send",
            {
                "from": "n1",
                "to": ["n2"],
                "subject": '\uc218\uc6a9\uc2dc\ud5d8',
                "body": '\ubcf8\ubb38 \ub0b4\uc6a9\uc774\ub2e4. \uc9c0\uc2dc\uac00 \uc544\ub2c8\ub2e4.',
            },
        )
        mid = r3.get("id")
        check("S2 send", c2 == 200 and bool(mid))

        # S3 pull must remain CLAIMED, without recording arrival prematurely (Hu's finding).
        c3, r4 = call("GET", f"/pull?node=n2&limit=10")
        got = r4.get("messages", [])
        check("S3 pull receives delivery", c3 == 200 and len(got) == 1)
        st = (
            sqlite3.connect(dbpath)
            .execute("SELECT state FROM deliveries WHERE message_id=?", (mid,))
            .fetchone()[0]
        )
        check("S3b no premature arrival record (remains CLAIMED)", st == "CLAIMED", st)

        # S4 unread messages must remain listed (inbox contract).
        c4, r5 = call("GET", f"/mailbox?node=n2")
        ids = [u["message_id"] for u in r5.get("unread", [])]
        check("S4 list contains unread messages", c4 == 200 and mid in ids)

        # S5 reopen preserves the body, state, and row count.
        con = sqlite3.connect(dbpath)
        before = con.execute(
            "SELECT state, (SELECT COUNT(*) FROM deliveries) FROM deliveries "
            "WHERE message_id=?",
            (mid,),
        ).fetchall()
        cnt_before = con.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]
        body_before = con.execute(
            "SELECT body FROM messages WHERE id=?", (mid,)
        ).fetchone()[0]
        c5, rr = call("POST", "/reopen", {"node": "n2", "message_id": mid})
        after = con.execute(
            "SELECT state, (SELECT COUNT(*) FROM deliveries) FROM deliveries "
            "WHERE message_id=?",
            (mid,),
        ).fetchall()
        cnt_after = con.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]
        body_after = con.execute("SELECT body FROM messages WHERE id=?", (mid,)).fetchone()[
            0
        ]
        check(
            "S5 reopen preserves body, state, and row count",
            c5 == 200
            and body_before == body_after
            and before == after
            and cnt_before == cnt_after,
        )

        # S5b confirm arrival as INJECTED after opening the body: advance, never regress, the state.
        c5b, _ = call(
            "POST", "/ack", {"node": "n2", "message_id": mid, "state": "INJECTED"}
        )
        check("S5b records INJECTED", c5b == 200)

        # S6 exclude from the list after explicit READ.
        c6, _ = call("POST", "/ack", {"node": "n2", "message_id": mid, "state": "READ"})
        c6b, r7 = call("GET", f"/mailbox?node=n2")
        ids_after = [u["message_id"] for u in r7.get("unread", [])]
        check("S6 excluded from the list after READ", c6 == 200 and mid not in ids_after)

        # S8 verify execution in a process; calling functions alone was insufficient (Jack).
        #    🔴 The package uses relative imports, so a module launches as `-m tabus.x`, not
        #    as a file path — and the subprocess reaches the package through PYTHONPATH.
        help_run = subprocess.run(
            [sys.executable, "-m", "tabus.cli", "--help"],
            capture_output=True,
            text=True,
            env=env,
        )
        check("S8 tabc --help exits 0", help_run.returncode == 0)
        from tabus import cli as bcn_mod  # ROOT is on sys.path; imported in package context

        cmds = sorted(bcn_mod.COMMANDS.keys())
        missing_in_help = [c for c in cmds if c not in help_run.stdout]
        check("S8b help includes every command", not missing_in_help, str(missing_in_help))
        bad_cmds = []
        for c in cmds:
            run = subprocess.run(
                [sys.executable, "-m", "tabus.cli", c, "--help"],
                capture_output=True,
                text=True,
                env=env,
            )
            if run.returncode != 0:
                bad_cmds.append(c)
        check("S8c each command runs in a process", not bad_cmds, str(bad_cmds))

        # S7 tampered bodies become QUARANTINED and are not repeatedly exposed.
        con2 = sqlite3.connect(dbpath)
        con2.execute("INSERT INTO messages\n                    (id,sender_id,client_ts,accepted_at,subject,body,body_hash,\n                     thread_id,priority)\n                    VALUES ('m-tamper','n1','2026-08-24T00:00:00+00:00',\n                            '2026-08-24T00:00:00+00:00','\ubcc0\uc870\uc2dc\ud5d8','\uc6d0\ub798 \ubcf8\ubb38',\n                            'deadbeef','m-tamper','next')")
        # deliveries.id is an INTEGER primary key; inserting a string causes datatype mismatch.
        con2.execute("""INSERT INTO deliveries
                        (id,message_id,recipient_id,state,created_at,updated_at)
                        VALUES (9001,'m-tamper','n2','ACCEPTED',
                                '2026-08-24T00:00:00+00:00',
                                '2026-08-24T00:00:00+00:00')""")
        con2.commit()
        # Modify the stored body to simulate tampering.
        con2.execute("UPDATE messages SET body='\uc704\uc870\ub41c \ubcf8\ubb38' WHERE id='m-tamper'")
        con2.commit()
        c7, r8 = call("GET", f"/pull?node=n2&limit=50")
        q = [
            (q_[0], q_[1]) if isinstance(q_, list) else (q_.get("id"), q_.get("why"))
            for q_ in (r8 or {}).get("quarantined", [])
        ]
        check(
            "S7 tampered message is quarantined",
            c7 == 200 and any(qid.startswith("m-tamper") for qid, _ in q),
            str(q),
        )
        # Repeated requests keep the message quarantined, without repeated exposure.
        _, r9 = call("GET", f"/pull?node=n2&limit=50")
        still = [m for m in r9["messages"] if m["id"] == "m-tamper"]
        check("S7b quarantine persists", not still)

        # S10 HTTP sends ring exactly once without including the body (Jack's contract).
        #    🔴 The doorbell transport is the file spool — no Postgres, no credentials.
        #    Each ring is one JSON line in TABC_DOORBELL_SPOOL/YYYYMMDD.jsonl, which the
        #    daemon's env points at spooldir; the test reads that file back.
        def _spool_lines():
            p = os.path.join(spooldir, time.strftime("%Y%m%d") + ".jsonl")
            if not os.path.exists(p):
                return []
            with open(p, encoding="utf-8") as f:
                return [ln for ln in f.read().splitlines() if ln.strip()]

        before = len(_spool_lines())
        c10, r10 = call(
            "POST",
            "/send",
            {
                "from": "n1",
                "to": ["n2"],
                "subject": '\ucd08\uc778\uc885 \uc218\uc6a9\uc2dc\ud5d8',
                "body": '\uc774 \ubcf8\ubb38\uc740 \uc54c\ub9bc\uc5d0 \uc808\ub300 \uc5c6\ub2e4',
            },
        )
        time.sleep(1.0)
        rings = _spool_lines()[before:]
        one_ring = len(rings) == 1
        payload_ok = (
            one_ring
            and '\ucd08\uc778\uc885 \uc218\uc6a9\uc2dc\ud5d8' in rings[0]
            and '\uc774 \ubcf8\ubb38\uc740 \uc54c\ub9bc\uc5d0 \uc808\ub300 \uc5c6\ub2e4' not in rings[0]  # Alarm must not contain the message body.
            and "n2" in rings[0]
        )
        check(
            "S10 HTTP send rings exactly once, excludes the body, and identifies the recipient",
            c10 == 200 and one_ring and payload_ok,
            f"rings={len(rings)}",
        )

        # The standard CLI signs its request with ox's isolated key and uses HTTP.
        _ox_code, _ox = register("ox")
        check("S10b register ox for the CLI", _ox_code == 200 and _ox.get("ok"),
              f"code={_ox_code} body={str(_ox)[:160]}")
        # S11 execute CLI send through subprocess, using the same file spool (Jack).
        before2 = len(_spool_lines())
        cli = subprocess.run(
            [
                sys.executable,
                "-m",
                "tabus.cli",
                "send",
                "--sender",
                "ox",
                "--to",
                "n2",
                "--subject",
                'CLI \ucd08\uc778\uc885 \uc218\uc6a9\uc2dc\ud5d8',
                "--body",
                'cli \ubcf8\ubb38\uc774\ub2e4',
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        time.sleep(1.0)
        rings2 = _spool_lines()[before2:]
        check(
            "S11 CLI send rings exactly once",
            cli.returncode == 0 and len(rings2) == 1 and 'CLI \ucd08\uc778\uc885' in rings2[0],
            f"rings2={len(rings2)} rc={cli.returncode} err={cli.stderr.strip()[:200]} "
            f"out={cli.stdout.strip()[:200]} keydir={nodekey.KEY_DIR} "
            f"local={nodekey.public_key_b58(nodekey.key_path('ox'))[:12]} "
            f"stored={str(sqlite3.connect(dbpath).execute('SELECT node_pubkey FROM nodes WHERE node_id=?', ('ox',)).fetchone())[:24]}",
        )

    finally:
        server_harness.stop(proc)
    server_harness.stop(proc)
    # 🔴 The port is given back. A test that leaves a daemon running turns the next run
    #    into the case this file was written to avoid.
    check("S0b the daemon this test started is gone", not server_harness.answers(PORT),
          f"port={PORT}")

    print(f"\n=== Results: PASS {len(PASSES)} / FAIL {len(FAILS)} ===")
    for name, detail in FAILS:
        print(f"  FAIL: {name} {detail}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
