"""Route proof and automatic-Enter preservation must compose without live state."""
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tabus import bus


class Integration(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(bus.SCHEMA)

    def tearDown(self):
        self.con.close()

    def register(self, **kwargs):
        return bus.bus_register(self.con, "alice", "codex", **kwargs)

    def route(self):
        return dict(self.con.execute("SELECT * FROM tab_routes WHERE node_id='alice'").fetchone())

    def test_verified_route_preserves_enter_and_rejects_legacy_move(self):
        fields = dict(adapter="tmux", target="socket\t%1", host_id="host")
        self.assertTrue(self.register(**fields, route_verified=True, auto_enter=True)[0])
        self.assertTrue(self.register(**fields)[0])
        self.assertEqual((self.route()["auto_enter"], self.route()["provenance_verified"]), (1, 1))
        before = self.route()
        self.assertFalse(self.register(adapter="tmux", target="socket\t%2", host_id="host")[0])
        self.assertEqual(self.route(), before)
        self.assertTrue(self.register(adapter="tmux", target="socket\t%2", host_id="host", route_verified=True)[0])
        self.assertEqual((self.route()["auto_enter"], self.route()["provenance_verified"]), (0, 1))

    def test_rejected_route_does_not_reset_surviving_verified_enter(self):
        fields = dict(adapter="tmux", target="socket\t%1", host_id="host")
        self.register(**fields, route_verified=True, auto_enter=True)
        meta = {}
        self.assertTrue(self.register(revoke_route=True, revoke_adapter="tmux",
                                      revoke_target="socket\t%1", revoke_host_id="host", result_meta=meta)[0])
        self.assertEqual(meta, {"route_active": True, "route_provenance_verified": True})
        self.assertEqual(self.route()["auto_enter"], 1)
        self.assertIsNone(self.route()["revoked_at"])
        self.register(auto_enter=False)
        self.assertEqual(self.route()["auto_enter"], 0)

    def test_migration_adds_proof_without_blessing_legacy_route(self):
        old = sqlite3.connect(":memory:")
        old.row_factory = sqlite3.Row
        try:
            old.executescript("""
                CREATE TABLE nodes(node_id TEXT PRIMARY KEY);
                CREATE TABLE tab_routes(node_id TEXT PRIMARY KEY, adapter TEXT,
                    target TEXT, host_id TEXT, registered_at TEXT, last_seen_at TEXT,
                    revoked_at TEXT, auto_enter INTEGER NOT NULL DEFAULT 0);
                INSERT INTO nodes VALUES ('legacy');
                INSERT INTO tab_routes VALUES ('legacy','tmux','socket','host','t0','t0',NULL,1);
            """)
            bus.migrate(old)
            bus.migrate(old)
            row = old.execute("SELECT provenance_verified, auto_enter FROM tab_routes").fetchone()
            self.assertEqual(tuple(row), (0, 1))
        finally:
            old.close()

    def test_failed_capture_preserves_legacy_route_and_enter(self):
        self.assertTrue(self.register(adapter="iterm2", target="old-pane",
                                     host_id="host", auto_enter=True)[0])
        before = self.route()
        meta = {}
        ok, message = self.register(revoke_route=True, revoke_adapter="iterm2",
                                    revoke_target="old-pane", revoke_host_id="host",
                                    result_meta=meta)
        self.assertTrue(ok, message)
        self.assertEqual(self.route(), before)
        self.assertEqual(meta, {"route_active": True, "route_provenance_verified": False})

    def test_failed_capture_does_not_restore_already_revoked_route(self):
        self.register(adapter="iterm2", target="old-pane", host_id="host")
        self.con.execute("UPDATE tab_routes SET revoked_at='previous-removal'")
        self.con.commit()
        before = self.route()
        self.register(revoke_route=True, revoke_adapter="iterm2",
                      revoke_target="old-pane", revoke_host_id="host")
        self.assertEqual(self.route(), before)

    def test_agent_cannot_become_program_and_state_is_unchanged(self):
        for verified in (False, True):
            with self.subTest(verified=verified):
                self.assertTrue(self.register(adapter="tmux", target="socket\t%1",
                    host_id="host", route_verified=verified, auto_enter=True)[0])
                before = list(self.con.iterdump())
                ok, message = self.register(program=True, auto_enter=False)
                self.assertFalse(ok)
                self.assertIn("existing agent cannot become a program", message)
                self.assertEqual(list(self.con.iterdump()), before)
                self.assertFalse(self.con.in_transaction)

    def test_route_less_agent_is_protected_but_program_registration_is_allowed(self):
        self.assertTrue(self.register()[0])
        before = list(self.con.iterdump())
        self.assertFalse(self.register(program=True)[0])
        self.assertEqual(list(self.con.iterdump()), before)
        for _ in range(2):
            self.assertTrue(bus.bus_register(self.con, "build-bot", "engine", program=True)[0])
        self.assertEqual(self.con.execute(
            "SELECT is_program FROM nodes WHERE node_id='build-bot'").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
