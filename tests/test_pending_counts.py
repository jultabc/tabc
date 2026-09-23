"""Sender-scoped pending counts, using only an in-memory database."""

import pathlib
import sqlite3
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from tabus import bus


class PendingCountsTest(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(bus.SCHEMA)
        bus.migrate(self.con)
        self.addCleanup(self.con.close)
        self.bell = patch.object(bus, "ring_doorbell", return_value=(True, None))
        self.bell.start()
        self.addCleanup(self.bell.stop)
        for node in ("alice", "bob", "carol", "dave"):
            bus.bus_register(self.con, node, "generic")

    def send(self, sender, recipients, **kwargs):
        mid, info = bus.bus_send(self.con, sender, recipients, "subject", "body", **kwargs)
        self.assertIsNotNone(mid, info)
        return mid

    def counts(self, sender):
        return {r["recipient"]: r["pending"] for r in bus.bus_pending_counts(self.con, sender)}

    def test_sender_and_tac_scope_with_readonly_global_query(self):
        self.send("alice", ["bob"])
        self.send("carol", ["bob"])
        self.send("carol", ["dave"])
        bus.bus_tac_create(self.con, "plan", "Plan", by="alice")
        plan = [row["tac_id"] for row in bus.bus_tac_list(self.con)
                if row.get("name") == "plan"][0]
        for node in ("alice", "bob", "dave"):
            bus.bus_tac_add(self.con, plan, node, by="alice")
        self.send("alice", [], tac_id=plan)
        before = list(map(tuple, self.con.execute("SELECT * FROM deliveries ORDER BY id")))
        self.assertEqual(self.counts("alice"), {"bob": 2, "dave": 1})
        self.assertEqual(self.counts("carol"), {"bob": 1, "dave": 1})
        self.assertEqual(self.counts("dave"), {})
        self.assertEqual(self.counts("nobody-at-all"), {})
        self.assertEqual(
            {r["recipient"]: r["pending"] for r in bus.bus_pending_counts_for_all(self.con)},
            {"bob": 3, "dave": 2},
        )
        self.assertEqual(before, list(map(tuple, self.con.execute("SELECT * FROM deliveries ORDER BY id"))))

    def test_broadcast_uses_actual_deliveries(self):
        self.send("alice", [], broadcast=True)
        self.assertEqual(self.counts("alice"), {"bob": 1, "carol": 1, "dave": 1})
        self.assertEqual(self.counts("carol"), {})

    def test_idempotent_retry_and_invalid_recipient(self):
        mid = self.send("alice", ["bob", "missing"])
        self.send("alice", ["bob", "missing"], message_id=mid)
        self.assertEqual(self.counts("alice"), {"bob": 1})

    def test_each_pending_state_and_read_recipient_omission(self):
        mid = self.send("alice", ["bob"])
        for state in ("ACCEPTED", "CLAIMED", "INJECTED"):
            with self.subTest(state=state):
                self.con.execute("UPDATE deliveries SET state=? WHERE message_id=?", (state, mid))
                self.assertEqual(self.counts("alice"), {"bob": 1})
        self.con.execute("UPDATE deliveries SET state='READ' WHERE message_id=?", (mid,))
        self.assertEqual(self.counts("alice"), {})


if __name__ == "__main__":
    unittest.main()
