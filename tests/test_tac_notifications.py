"""TAC notification eligibility, scoped counts, and terminal rendering."""
import os
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tabus import bus, doorbell, doorbell_poll


class TacNotifications(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(bus.SCHEMA)
        for name in ("alice", "bob", "carol"):
            bus.bus_register(self.con, name, "generic")
        bus.bus_tac_create(self.con, "dev", by="alice")
        self.tac = [row["tac_id"] for row in bus.bus_tac_list(self.con)
                    if row.get("name") == "dev"][0]
        for name in ("alice", "bob"):
            bus.bus_tac_add(self.con, self.tac, name, by="alice")
        self.ringer = patch.object(bus, "ring_doorbell", return_value=(True, None))
        self.ring = self.ringer.start()

    def tearDown(self):
        self.ringer.stop()
        self.con.close()

    def test_tac_only_notifies_delivered_members(self):
        mid, result = bus.bus_send(self.con, "alice", [], "topic", "body", tac_id=self.tac)
        self.assertIsNotNone(mid)
        self.assertEqual(result["recipients"], ["bob"])
        self.assertTrue(result["doorbell_ok"])
        self.assertEqual(self.ring.call_args.args[0], "bob")
        self.assertEqual([row[0] for row in doorbell_poll.scan_unread(self.con)], ["bob"])
        bus.bus_set_snooze(self.con, "bob", 60)
        self.assertEqual(doorbell_poll.scan_unread(self.con), [])
        bus.bus_set_snooze(self.con, "bob", 0)
        bus.bus_remove(self.con, "bob", by="alice")
        self.assertEqual(doorbell_poll.scan_unread(self.con), [])

    def test_mixed_scope_counts_and_rendering(self):
        bus.bus_send(self.con, "alice", ["bob"], "direct", "body")
        for index in range(2):
            bus.bus_send(self.con, "alice", [], str(index), "body", tac_id=self.tac)
        groups = bus.bus_unread_senders(self.con, "bob", include_tac=True)
        self.assertEqual({row["tac_id"]: row["count"] for row in groups},
                         {None: 1, self.tac: 2})
        with patch.object(doorbell, "unread_senders", return_value=groups), \
             patch.object(doorbell, "route_for", return_value=("iterm2", "fake", True)), \
             patch.object(doorbell, "send_to_iterm_session", return_value=("SUCCESS", 1)) as send, \
             patch.dict(os.environ, {"TABC_LANG": "en"}):
            doorbell.deliver_doorbell("bob", 3)
        text = send.call_args.args[1]
        self.assertIn("[dm] 1 unread · from: alice", text)
        self.assertIn("[tac] dev · 2 unread · from: alice", text)
        self.assertNotIn("body", text)

    def test_two_senders_in_one_tac_show_latest_senders_count(self):
        for index in range(2):
            mid, _ = bus.bus_send(self.con, "alice", [], str(index), "body", tac_id=self.tac)
            self.assertIsNotNone(mid)
        bus.bus_tac_add(self.con, self.tac, "carol", by="alice")
        mid, _ = bus.bus_send(self.con, "carol", [], "latest", "body", tac_id=self.tac)
        self.assertIsNotNone(mid)
        groups = bus.bus_unread_senders(self.con, "bob", include_tac=True)
        self.assertEqual({g["sender"]: g["count"] for g in groups}, {"alice": 2, "carol": 1})
        with patch.object(doorbell, "unread_senders", return_value=groups), \
             patch.object(doorbell, "route_for", return_value=("iterm2", "fake", True)), \
             patch.object(doorbell, "send_to_iterm_session", return_value=("SUCCESS", 1)) as send, \
             patch.dict(os.environ, {"TABC_LANG": "en"}):
            doorbell.deliver_doorbell("bob", 3)
        self.assertEqual(send.call_args.args[1], "[tac] dev · 1 unread · from: carol")

    def test_same_timestamp_uses_latest_delivery_not_largest_count(self):
        for index in range(2):
            bus.bus_send(self.con, "alice", ["bob"], str(index), "body")
        bus.bus_send(self.con, "carol", ["bob"], "newest", "body")
        self.con.execute("UPDATE deliveries SET created_at='2026-09-05T00:00:00+00:00'")
        groups = bus.bus_unread_senders(self.con, "bob", include_tac=True)
        self.assertEqual((groups[0]["sender"], groups[0]["count"]), ("carol", 1))


if __name__ == "__main__":
    unittest.main()
