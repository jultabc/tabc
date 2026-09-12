#!/usr/bin/env python3
"""Beacon database connection lifecycle regression tests; temporary databases only."""

import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="tabus_conn_test_")
DB_PATH = os.path.join(TMP, "tabus.db")
LEDGER_PATH = os.path.join(TMP, "doorbell_ring.db")
os.environ["TABC_DB"] = DB_PATH
os.environ["DOORBELL_LEDGER"] = LEDGER_PATH
os.environ["TABC_HOME"] = TMP  # keys + state isolated to the temp dir, off real ~/.tabc
sys.path.insert(0, ROOT)

# 🔴 the module, not the package. This file reaches into the implementation —
#    it replaces functions on it, or reads names that begin with an underscore.
#    Re-export copies public names only, and an attribute set on the package is
#    invisible to callers inside the module.
from tabus import bus as tabus  # noqa: E402
from tabus import doorbell_poll as doorbell_poll  # noqa: E402
from tabus import daemon as tabd  # noqa: E402
from tabus import nodekey  # noqa: E402

fails = []


def check(name, condition):
    print(f"  {'PASS' if condition else 'FAIL'}  {name}")
    if not condition:
        fails.append(name)


class QuietServer(tabd.ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass


def request_cleanup_test():
    tabd.init_extras()
    retained = []
    original = tabus.bus_register

    def fail_after_write(con, *args, **kwargs):
        con.execute(
            "INSERT INTO mailbox_opens(node, opened_at) VALUES ('lock-owner', 'now')"
        )
        retained.append(con)
        raise RuntimeError('\uc4f0\uae30 \ub4a4 \uc758\ub3c4\ub41c \uc2e4\ud328')

    tabus.bus_register = fail_after_write
    server = QuietServer(("127.0.0.1", 0), tabd.BusHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # 🔴 Signed bootstrap register: the body carries probe's pubkey and the
        #    request is signed by probe's key, so _authorized passes and the handler
        #    (fail_after_write) runs and raises — the failure this test exercises.
        keypath = nodekey.key_path("probe")
        pub = nodekey.public_key_b58(keypath)
        body = json.dumps({"node": "probe", "pubkey": pub}, ensure_ascii=False)
        ts = str(int(time.time()))
        sig = nodekey.b58encode(
            nodekey.sign(
                nodekey.canonical_request("probe", "POST", "/register", body, ts),
                keypath,
            )
        )
        req = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/register",
            data=body.encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Node": "probe",
                "X-Node-Ts": ts,
                "X-Node-Sig": sig,
            },
        )
        try:
            urllib.request.urlopen(req, timeout=2).read()
        except (urllib.error.URLError, ConnectionError):
            pass

        probe = sqlite3.connect(DB_PATH, timeout=0.1)
        try:
            probe.execute(
                "INSERT INTO mailbox_opens(node, opened_at) VALUES ('next-writer', 'now')"
            )
            probe.commit()
            writable = True
        except sqlite3.OperationalError:
            writable = False
        finally:
            probe.close()
        check("request exception rolls back and closes the unfinished write connection", writable)
        check("retain the failed connection object during the test", bool(retained))
    finally:
        tabus.bus_register = original
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class TrackedConnection:
    def __init__(self, con, label, closed):
        self._con = con
        self._label = label
        self._closed = closed

    def __getattr__(self, name):
        return getattr(self._con, name)

    def __enter__(self):
        self._con.__enter__()
        return self

    def __exit__(self, *args):
        return self._con.__exit__(*args)

    def close(self):
        self._con.close()
        self._closed.append(self._label)


def doorbell_cleanup_test():
    con = tabus.connect()
    con.executescript(tabus.SCHEMA)
    tabus.migrate(con)
    con.close()

    closed = []
    original_ro = doorbell_poll._tabus_ro
    original_ledger = doorbell_poll.ledger
    original_scan = doorbell_poll.scan_unread
    doorbell_poll._tabus_ro = lambda: TrackedConnection(
        original_ro(), "tabus", closed
    )
    doorbell_poll.ledger = lambda: TrackedConnection(
        original_ledger(), "ledger", closed
    )
    try:
        doorbell_poll.decide("SHADOW")
        check("alarm iteration closes the tabus read connection", closed.count("tabus") == 1)
        check("alarm iteration closes the ledger connection", closed.count("ledger") == 1)

        doorbell_poll.record_ring("probe", 1, 1, "SHADOW", "SHADOW_ONLY")
        doorbell_poll.record_ring("probe", 1, 1, "SHADOW", "SHADOW_ONLY")
        check("duplicate alarm early return also closes the connection", closed.count("ledger") == 3)

        def fail_scan(_con):
            raise RuntimeError('\uc758\ub3c4\ub41c \ud68c\ucc28 \uc2e4\ud328')

        doorbell_poll.scan_unread = fail_scan
        try:
            doorbell_poll.decide("SHADOW")
        except RuntimeError:
            pass
        check("failed iteration also closes the tabus read connection", closed.count("tabus") == 2)
        check("failed iteration closes both its ledger and the error ledger connection", closed.count("ledger") == 5)
    finally:
        doorbell_poll._tabus_ro = original_ro
        doorbell_poll.ledger = original_ledger
        doorbell_poll.scan_unread = original_scan


request_cleanup_test()
doorbell_cleanup_test()
print(f"\n{'FAIL' if fails else 'ALL PASS'} — {len(fails)} failed")
sys.exit(1 if fails else 0)
