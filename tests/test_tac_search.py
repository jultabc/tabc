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
from tabus import bus, cli, tac_store
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
        self.ids = {}
        for name in ("visible", "secret"):
            bus.bus_tac_create(self.con, name, '\uac80\uc0c9 label', by="alice")
            tac_id = [row["tac_id"] for row in bus.bus_tac_list(self.con)
                      if row["name"] == name][0]
            self.ids[name] = tac_id
            bus.bus_tac_add(self.con, tac_id, "alice", by="alice")
            bus.bus_tac_add(self.con, tac_id, "bob", by="alice")
            bus.bus_send(self.con, "alice", [], "Title", '\ubcf8\ubb38 100%_ & caf\xe9', tac_id=tac_id)
        bus.bus_tac_remove_member(self.con, self.ids["secret"], "bob", by="alice")
        bus.bus_tac_close(self.con, self.ids["visible"], "closing decision", by="alice")

    def test_membership_and_closed_tac(self):
        self.assertEqual({r["tac_id"] for r in search(self.con, "bob", '\ubcf8\ubb38')},
                         {self.ids["visible"]})
        self.assertEqual(search(self.con, "outsider", '\ubcf8\ubb38'), [])
        self.assertEqual(search(self.con, "bob", '\ubcf8\ubb38', self.ids["secret"]), [])
        self.assertEqual(search(self.con, "bob", "decision")[0]["kind"], "tac")
        self.assertEqual(search(self.con, "bob", '\uac80\uc0c9')[0]["kind"], "tac")
        self.assertEqual(search(self.con, "bob", "title")[0]["kind"], "message")
        self.assertEqual(search(self.con, "bob", "CAFE\u0301")[0]["kind"], "message")

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

    def test_converted_tac_name_uses_unicode_folded_key(self):
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.executescript(bus.SCHEMA)
        bus.migrate(con)
        tac_store.ensure_columns(con)
        self.addCleanup(con.close)
        for node in ("alice", "bob"):
            bus.bus_register(con, node, "codex")
        ok, _ = bus.bus_tac_create(con, "Caf\u00e9 \ud300", by="alice")
        self.assertTrue(ok)
        tac_id = bus.bus_tac_list(con)[0]["tac_id"]
        bus.bus_tac_add(con, tac_id, "alice", by="alice")
        bus.bus_tac_add(con, tac_id, "bob", by="alice")

        hits = search(con, "bob", "CAFE\u0301 \ud300")

        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["tac_id"], tac_id)
        self.assertEqual(hits[0]["subject"], "Caf\u00e9 \ud300")

    def test_cli_encoding_and_error(self):
        args = cli.build_parser().parse_args([
            "tac", "search", self.ids["visible"], "--node", "bob", "--query", '\ud55c\uae00 & 100%_'
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
