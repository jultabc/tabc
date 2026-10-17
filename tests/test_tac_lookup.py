#!/usr/bin/env python3
"""Finding a tac by name (0.2.3).

Pins:
- The server still designates by identifier. /tac_lookup only says which tac carries a
  name now; every other endpoint refuses a name as before.
- An exact name of an open tac becomes that identifier, and the client prints it.
- A closed tac, or no exact name, lists candidates with identifier and open/closed and
  exits 2 without a request. A partial name is never chosen for the caller.
- A canonical UUID makes no lookup. Another UUID spelling is asked as a name first; with
  no tac of that exact name it is the same identifier.
- A server without the endpoint (404) gets the string as typed, as before.
"""

import contextlib
import io
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

os.environ.setdefault("TABC_DB", os.path.join(tempfile.mkdtemp(prefix="taclookup_"), "t.db"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tabus import bus, cli  # noqa: E402
from tabus.tac_lookup import lookup  # noqa: E402


def ledger():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(bus.SCHEMA)
    bus.migrate(con)
    bus.bus_register(con, "alice", "generic")
    for name in ("loom", "loom-watchd", "market-db"):
        bus.bus_tac_create(con, name, None, by="alice")
    con.commit()
    return con


def ids(con):
    return {r["name"]: r["tac_id"] for r in bus.bus_tac_list(con)}


class Lookup(unittest.TestCase):
    def setUp(self):
        self.con = ledger()
        self.addCleanup(self.con.close)

    def test_exact_is_folded_and_not_repeated_in_similar(self):
        exact, similar = lookup(self.con, "LOOM")
        self.assertEqual(exact["tac_id"], ids(self.con)["loom"])
        self.assertIsNone(exact["closed_at"])
        self.assertEqual([r["name"] for r in similar], ["loom-watchd"])

    def test_closed_tac_reports_closed_at(self):
        bus.bus_tac_close(self.con, ids(self.con)["loom"], "done", by="alice")
        exact, _ = lookup(self.con, "loom")
        self.assertTrue(exact["closed_at"])

    def test_partial_name_is_not_exact(self):
        exact, similar = lookup(self.con, "loo")
        self.assertIsNone(exact)
        self.assertEqual([r["name"] for r in similar], ["loom", "loom-watchd"])

    def test_no_match(self):
        self.assertEqual(lookup(self.con, "nothing"), (None, []))

    def test_bad_query(self):
        for bad in (None, "", "   ", "x" * 201, 5):
            with self.assertRaises(ValueError):
                lookup(self.con, bad)

    def test_similar_is_capped(self):
        for i in range(25):
            bus.bus_tac_create(self.con, f"cap-{i:02d}", None, by="alice")
        self.assertEqual(len(lookup(self.con, "cap", limit=20)[1]), 20)

    def test_ledger_without_names(self):
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.execute("CREATE TABLE tacs (tac_id TEXT PRIMARY KEY, label TEXT, created_at TEXT, closed_at TEXT)")
        self.assertEqual(lookup(con, "loom"), (None, []))

    def test_server_designation_is_unchanged(self):
        # 🔴 The lookup adds no name designation to the server.
        from tabus import tac_store
        _, refusal = tac_store.resolve(self.con, "loom")
        self.assertEqual(refusal.fields()["code"], "TAC_ID_INVALID")


UUID = "2d8be2cc-4168-4764-bed0-09909ec27aad"


class Designate(unittest.TestCase):
    def run_designate(self, given, reply=(200, {}), calls=None):
        calls = [] if calls is None else calls

        def fake(method, path, payload=None, node=None):
            calls.append((method, path, node))
            return reply
        err = io.StringIO()
        with mock.patch.object(cli, "call", fake), contextlib.redirect_stderr(err):
            try:
                out = cli._designate_tac(given, "alice")
                code = None
            except SystemExit as e:
                out, code = None, e.code
        return out, code, err.getvalue(), calls

    def test_canonical_uuid_makes_no_request(self):
        out, code, _, calls = self.run_designate(UUID)
        self.assertEqual((out, code, calls), (UUID, None, []))

    def test_other_uuid_spelling_with_no_such_name_is_the_identifier(self):
        none = (200, {"exact": None, "similar": []})
        for spelling in (UUID.upper(), "{" + UUID + "}", UUID.replace("-", "")):
            out, code, _, calls = self.run_designate(spelling, none)
            self.assertEqual((out, code), (UUID, None), spelling)
            self.assertEqual(len(calls), 1, spelling)  # the name was asked first

    def test_name_that_reads_as_uuid_is_the_name(self):  # jack 4c84df2c
        other = "44444444-4444-4444-8444-444444444444"
        hexname = other.replace("-", "")
        reply = (200, {"exact": {"tac_id": UUID, "name": hexname, "closed_at": None}, "similar": []})
        out, code, err, _ = self.run_designate(hexname, reply)
        self.assertEqual((out, code), (UUID, None))  # the tac named so, not tac `other`
        self.assertIn(f"tac {hexname} = {UUID}", err)

    def test_uuid_spelling_on_old_server_is_sent_as_typed(self):
        out, _, _, _ = self.run_designate(UUID.upper(), (404, {}))
        self.assertEqual(out, UUID.upper())

    def test_exact_open_name_is_used_and_printed(self):
        reply = (200, {"exact": {"tac_id": UUID, "name": "loom", "closed_at": None}, "similar": []})
        out, code, err, calls = self.run_designate("loom", reply)
        self.assertEqual((out, code), (UUID, None))
        self.assertIn(f"tac loom = {UUID}", err)
        method, path, node = calls[0]
        self.assertEqual((method, urlsplit(path).path, node), ("GET", "/tac_lookup", "alice"))
        self.assertEqual(parse_qs(urlsplit(path).query), {"name": ["loom"]})

    def test_closed_exact_lists_and_exits_2(self):
        reply = (200, {"exact": {"tac_id": UUID, "name": "loom", "closed_at": "2026-10-02T01:00:00Z"},
                       "similar": [{"tac_id": "1" * 8 + "-1111-4111-8111-" + "1" * 12, "name": "loom-watchd",
                                    "closed_at": None}]})
        out, code, err, _ = self.run_designate("loom", reply)
        self.assertEqual((out, code), (None, 2))
        self.assertIn("tac loom is closed; nothing was done", err)
        self.assertIn(f"loom                     {UUID}  closed 2026-10-02", err)
        self.assertIn("loom-watchd", err)
        self.assertIn("open", err)
        self.assertIn("code TAC_NAME_UNRESOLVED", err)

    def test_no_exact_lists_similar_and_exits_2(self):
        reply = (200, {"exact": None, "similar": [{"tac_id": UUID, "name": "loom", "closed_at": None}]})
        out, code, err, _ = self.run_designate("loo", reply)
        self.assertEqual((out, code), (None, 2))
        self.assertIn("no tac is named 'loo'; nothing was done. Similar names:", err)
        self.assertIn(f"{UUID}  open", err)

    def test_nothing_at_all_exits_2(self):
        out, code, err, _ = self.run_designate("zzz", (200, {"exact": None, "similar": []}))
        self.assertEqual((out, code), (None, 2))
        self.assertIn("list them with: tabc tac ls", err)

    def test_old_server_gets_the_string_as_typed(self):
        out, code, _, _ = self.run_designate("loom", (404, {"error": "not found"}))
        self.assertEqual((out, code), ("loom", None))

    def test_lookup_failure_exits_1(self):
        out, code, err, _ = self.run_designate("loom", (500, {"error": "boom"}))
        self.assertEqual((out, code), (None, 1))
        self.assertIn("tac lookup: boom", err)

    def test_empty_passes_through_for_usage_messages(self):
        for given in (None, "", "  "):
            out, code, _, calls = self.run_designate(given)
            self.assertEqual((out, code, calls), (given, None, []))


class Wiring(unittest.TestCase):
    """Each command that takes an existing tac sends the identifier."""

    def run_cli(self, argv, replies):
        sent = []

        def fake(method, path, payload=None, node=None):
            sent.append((method, path, payload))
            if path.startswith("/tac_lookup"):
                name = parse_qs(urlsplit(path).query)["name"][0]
                return 200, replies[name]
            if path.startswith("/tac_messages"):
                return 200, {"exists": True, "tac": UUID, "name": "loom", "members": [], "messages": []}
            return 200, {"ok": True, "msg": "ok", "id": "x", "delivered": [], "to": []}
        args = cli.build_parser().parse_args(argv)
        with mock.patch.object(cli, "call", fake), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()), mock.patch.dict(os.environ, {"TABC_NODE": "alice"}):
            try:
                cli.COMMANDS[args.cmd][0](args)
            except SystemExit as e:
                if e.code not in (0, None):
                    raise
        return sent

    OPEN = {"exact": {"tac_id": UUID, "name": "loom", "closed_at": None}, "similar": []}
    PARENT = "33333333-3333-4333-8333-333333333333"

    def test_show_uses_identifier(self):
        sent = self.run_cli(["tac", "show", "loom", "--node", "alice"], {"loom": self.OPEN})
        paths = [p for _, p, _ in sent if p.startswith("/tac_messages")]
        self.assertEqual(len(paths), 1)
        self.assertIn(f"tac={UUID}", paths[0])

    def test_add_rm_close_use_identifier(self):
        for argv, path in ((["tac", "add", "loom", "bob"], "/tac_add"),
                           (["tac", "rm", "loom", "bob"], "/tac_remove"),
                           (["tac", "close", "loom", "--summary", "s"], "/tac_close")):
            sent = self.run_cli(argv + ["--node", "alice"], {"loom": self.OPEN})
            body = [b for _, p, b in sent if p == path][0]
            self.assertEqual(body["tac"], UUID, argv)

    def test_rename_resolves_the_tac_not_the_new_name(self):
        sent = self.run_cli(["tac", "rename", "loom", "loom2", "--node", "alice"], {"loom": self.OPEN})
        body = [b for _, p, b in sent if p == "/tac_rename"][0]
        self.assertEqual((body["tac"], body["name"]), (UUID, "loom2"))
        self.assertEqual(len([p for _, p, _ in sent if p.startswith("/tac_lookup")]), 1)

    def test_link_resolves_child_and_parent(self):
        parent = {"exact": {"tac_id": self.PARENT, "name": "market-db", "closed_at": None}, "similar": []}
        sent = self.run_cli(["tac", "link", "loom", "market-db", "--node", "alice"],
                            {"loom": self.OPEN, "market-db": parent})
        body = [b for _, p, b in sent if p == "/tac_link"][0]
        self.assertEqual((body["child"], body["parent"]), (UUID, self.PARENT))

    def test_create_is_not_looked_up(self):
        sent = self.run_cli(["tac", "create", "loom", "--node", "alice"], {})
        self.assertFalse([p for _, p, _ in sent if p.startswith("/tac_lookup")])

    def test_send_tac_uses_identifier(self):
        sent = self.run_cli(["send", "--sender", "alice", "--tac", "loom", "--subject", "s", "--body", "b"],
                            {"loom": self.OPEN})
        body = [b for _, p, b in sent if p == "/send"][0]
        self.assertEqual(body["tac"], UUID)

    def test_closed_name_sends_nothing(self):
        closed = {"exact": {"tac_id": UUID, "name": "loom", "closed_at": "2026-10-02T00:00:00Z"}, "similar": []}
        with self.assertRaises(SystemExit) as cm:
            self.run_cli(["send", "--sender", "alice", "--tac", "loom", "--subject", "s", "--body", "b"],
                         {"loom": closed})
        self.assertEqual(cm.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
