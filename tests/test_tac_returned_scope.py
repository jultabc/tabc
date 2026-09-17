"""Catch-up must not advance deliveries outside the returned selection."""

import unittest

import tabus


class ReturnedScopeTest(unittest.TestCase):
    def setUp(self):
        import sqlite3

        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(tabus.SCHEMA)
        tabus.migrate(self.con)
        for node in ("sender", "reader", "other"):
            tabus.bus_register(self.con, node, "generic")
        for topic in ("first", "second"):
            tabus.bus_tac_create(self.con, topic)
            for node in ("sender", "reader", "other"):
                tabus.bus_tac_add(self.con, topic, node)
        self.ids = [
            tabus.bus_send(self.con, "sender", [], str(i), "body", tac_id="first")[0]
            for i in range(3)
        ]
        self.foreign = tabus.bus_send(
            self.con, "sender", [], "foreign", "body", tac_id="second"
        )[0]

    def tearDown(self):
        self.con.close()

    def state(self, message, node="reader"):
        return self.con.execute(
            "SELECT state FROM deliveries WHERE recipient_id=? AND message_id=?",
            (node, message),
        ).fetchone()[0]

    def test_limit_selection_and_repeat(self):
        page = tabus.bus_tac_messages(self.con, "first", limit=1)
        ids = [m["id"] for m in page]
        self.assertEqual(1, tabus.bus_tac_mark_read(self.con, "reader", "first", ids))
        self.assertEqual(0, tabus.bus_tac_mark_read(self.con, "reader", "first", ids))
        for message in self.ids:
            self.assertEqual("INJECTED" if message in ids else "ACCEPTED", self.state(message))
            self.assertEqual("ACCEPTED", self.state(message, "other"))
        sent, error = tabus.bus_send(self.con, "reader", [], "reply", "body", tac_id="first")
        self.assertIsNone(sent)
        self.assertIn("unread", error)

    def test_empty_and_cross_topic_selection(self):
        self.assertEqual(0, tabus.bus_tac_mark_read(self.con, "reader", "first", []))
        self.assertEqual(0, tabus.bus_tac_mark_read(self.con, "reader", "first", [self.foreign]))
        self.assertTrue(all(self.state(m) == "ACCEPTED" for m in self.ids))
        self.assertEqual("ACCEPTED", self.state(self.foreign))


if __name__ == "__main__":
    unittest.main()
