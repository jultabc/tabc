"""Search scope, literal matching, CLI encoding, and read-only behavior."""
import contextlib
import io
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tabus import bus, cli
from tabus.tac_search import search


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(bus.SCHEMA)
        bus.migrate(self.con)
        self.addCleanup(self.con.close)
        for node in ("alice", "bob", "outsider"):
            bus.bus_register(self.con, node, "codex")
        for tac in ("visible", "secret"):
            bus.bus_tac_create(self.con, tac, '\uac80\uc0c9 label', by="alice")
            bus.bus_tac_add(self.con, tac, "alice", by="alice")
            bus.bus_tac_add(self.con, tac, "bob", by="alice")
            bus.bus_send(self.con, "alice", [], "Title", '\ubcf8\ubb38 100%_ & caf\xe9', tac_id=tac)
        bus.bus_tac_remove_member(self.con, "secret", "bob", by="alice")
        bus.bus_tac_close(self.con, "visible", "closing decision", by="alice")

    def test_membership_and_closed_tac(self):
        self.assertEqual({r["tac_id"] for r in search(self.con, "bob", '\ubcf8\ubb38')}, {"visible"})
        self.assertEqual(search(self.con, "outsider", '\ubcf8\ubb38'), [])
        self.assertEqual(search(self.con, "bob", '\ubcf8\ubb38', "secret"), [])
        self.assertEqual(search(self.con, "bob", "decision")[0]["kind"], "tac")
        self.assertEqual(search(self.con, "bob", '\uac80\uc0c9')[0]["kind"], "tac")
        self.assertEqual(search(self.con, "bob", "title")[0]["kind"], "message")

    def test_read_only_literals_limits(self):
        before = list(self.con.iterdump())
        self.assertEqual(len(search(self.con, "bob", "%_")), 1)
        self.assertEqual(search(self.con, "bob", "' OR 1=1 --"), [])
        self.assertEqual(len(search(self.con, "alice", '\ubcf8\ubb38', limit=1)), 1)
        self.assertEqual(list(self.con.iterdump()), before)
        for query in (None, "", "  ", "a" * 501):
            with self.assertRaises(ValueError):
                search(self.con, "bob", query)
        for limit in (0, -1, 201, True):
            with self.assertRaises(ValueError):
                search(self.con, "bob", '\ubcf8\ubb38', limit=limit)

    def test_removed_node(self):
        bus.bus_remove(self.con, "bob")
        self.assertEqual(search(self.con, "bob", '\ubcf8\ubb38'), [])

    def test_cli_encoding_and_error(self):
        args = cli.build_parser().parse_args([
            "tac", "search", "visible", "--node", "bob", "--query", '\ud55c\uae00 & 100%_'
        ])
        with patch.object(cli, "call", return_value=(200, {"hits": []})) as call, \
                contextlib.redirect_stdout(io.StringIO()):
            cli.fn_tac(args)
        self.assertEqual(call.call_args.args[0], "GET")
        self.assertEqual(call.call_args.kwargs["node"], "bob")
        self.assertEqual(parse_qs(urlsplit(call.call_args.args[1]).query)["q"], [args.query])
        with patch.object(cli, "call", return_value=(403, {"error": "denied"})), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            cli.fn_tac(args)


if __name__ == "__main__":
    unittest.main()
