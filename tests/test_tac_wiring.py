#!/usr/bin/env python3
"""The bus with tac identifiers separated from tac names.

Pins:
- Two states live at once. A ledger that has not been converted behaves as it did: the
  string a caller types is the identifier. A converted one mints a UUID for each tac.
- 🔴 Designation is by identifier only. After the conversion a name designates nothing,
  and neither does the identifier a tac carried before it. Names are for creating and
  for searching.
- A fresh 0.2.0 ledger starts on UUIDs. Opening an older ledger never adds the
  identity columns; only the conversion tool does.
- Messages, members and links hold the identifier, so a rename detaches nothing.
- Refusals carry a code, and the sentence a caller reads is unchanged.
"""

import os
import sqlite3
import sys
import tempfile
import unittest

os.environ.setdefault("TABC_DB", os.path.join(tempfile.mkdtemp(prefix="tacwire_"), "t.db"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tabus import bus  # noqa: E402
from tabus.tac_identity import is_uuid_id  # noqa: E402


def ledger(converted=True):
    """A fresh 0.2.0 ledger or a legacy ledger opened by 0.2.0."""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    if not converted:
        # A table created by an earlier release. CREATE IF NOT EXISTS in the new
        # schema must not add identity columns to it.
        con.execute("""CREATE TABLE tacs (
            tac_id TEXT PRIMARY KEY, label TEXT, created_at TEXT NOT NULL,
            created_by TEXT, closed_at TEXT, close_summary TEXT, closed_by TEXT
        )""")
    con.executescript(bus.SCHEMA)
    bus.migrate(con)
    columns = {r[1] for r in con.execute("PRAGMA table_info(tacs)")}
    assert ("name" in columns) is converted
    for node in ("alice", "bob", "carol"):
        bus.bus_register(con, node, "generic")
    con.commit()
    return con


class BeforeTheConversion(unittest.TestCase):
    """A ledger without the identity columns keeps the behaviour it had."""

    def setUp(self):
        self.con = ledger(converted=False)
        self.addCleanup(self.con.close)

    def test_the_string_stays_the_identifier(self):
        ok, msg = bus.bus_tac_create(self.con, "beacon-alert", by="alice")
        self.assertTrue(ok, msg)
        self.assertEqual(
            self.con.execute("SELECT tac_id FROM tacs").fetchone()[0], "beacon-alert"
        )
        self.assertEqual(bus.tac_resolve(self.con, "beacon-alert")[0], "beacon-alert")
        for node in ("alice", "bob"):
            self.assertTrue(bus.bus_tac_add(self.con, "beacon-alert", node, by="alice")[0])
        mid, err = bus.bus_send(self.con, "alice", [], "s", "b", tac_id="beacon-alert")
        self.assertIsNotNone(mid, err)
        self.assertEqual(
            self.con.execute("SELECT tac_id FROM messages WHERE id=?", (mid,)).fetchone()[0],
            "beacon-alert",
        )

    def test_the_scheme_says_the_columns_are_missing(self):
        self.assertEqual(bus.tac_scheme(self.con), {"converted": False, "unconverted": None})

    def test_a_ledger_with_old_rows_refuses_to_mint(self):
        # 🔴 The window an existing installation sits in once the columns are there and
        #    the rows are not converted yet: reading and sending keep working, and only
        #    create and rename stop rather than mix two kinds of identifier in one ledger.
        bus.bus_tac_create(self.con, "beacon-alert", by="alice")
        from tabus.tac_store import ensure_columns

        ensure_columns(self.con)
        for node in ("alice", "bob"):
            self.assertTrue(bus.bus_tac_add(self.con, "beacon-alert", node, by="alice")[0])
        mid, err = bus.bus_send(self.con, "alice", [], "s", "b", tac_id="beacon-alert")
        self.assertIsNotNone(mid, err)
        self.assertEqual(
            [m["id"] for m in bus.bus_tac_messages(self.con, "beacon-alert")], [mid]
        )
        ok, msg = bus.bus_tac_create(self.con, "tac-uuid", by="alice")
        self.assertFalse(ok)
        self.assertEqual(msg.code, "TAC_NOT_CONVERTED")
        self.assertIn("migration", msg)
        ok, msg = bus.bus_tac_rename(self.con, "beacon-alert", "other", by="alice")
        self.assertFalse(ok)
        self.assertEqual(msg.code, "TAC_NOT_CONVERTED")


class AfterTheConversion(unittest.TestCase):
    def setUp(self):
        self.con = ledger()
        self.addCleanup(self.con.close)
        ok, msg = bus.bus_tac_create(self.con, "beacon-alert", "doorbell work", by="alice")
        self.assertTrue(ok, msg)
        self.tac = self.con.execute("SELECT tac_id FROM tacs").fetchone()[0]
        for node in ("alice", "bob"):
            bus.bus_tac_add(self.con, self.tac, node, by="alice")

    def test_create_mints_the_identifier_and_keeps_the_description(self):
        self.assertTrue(is_uuid_id(self.tac))
        row = self.con.execute(
            "SELECT name, label FROM tacs WHERE tac_id=?", (self.tac,)
        ).fetchone()
        self.assertEqual((row["name"], row["label"]), ("beacon-alert", "doorbell work"))

    def test_only_the_identifier_designates(self):
        # 🔴 The window is closed: a name and a previous identifier both designate
        #    nothing once the ledger is converted. Finding a tac by name is what
        #    tac_search is for, and it answers with the identifier.
        self.con.execute(
            "INSERT INTO tac_legacy_ids(old_id, tac_id, migrated_at) VALUES(?,?,?)",
            ("beacon", self.tac, "2026-09-18T00:00:00"),
        )
        self.assertEqual(bus.tac_resolve(self.con, self.tac)[0], self.tac)
        for spelling in ("beacon-alert", "BEACON-ALERT", "beacon", "", None):
            designated, refusal = bus.tac_resolve(self.con, spelling)
            self.assertIsNone(designated, repr(spelling))
            self.assertEqual(refusal.code, "TAC_ID_INVALID", repr(spelling))
            self.assertEqual(refusal.retry, "never")
        # An identifier that is canonical and absent is a different fact.
        absent = "0b3f1f2e-8a3c-4d5e-9f10-1a2b3c4d5e6f"
        self.assertEqual(bus.tac_resolve(self.con, absent)[0], absent)
        ok, msg = bus.bus_tac_add(self.con, absent, "carol", by="alice")
        self.assertFalse(ok)
        self.assertEqual(msg.code, "TAC_NOT_FOUND")

    def test_sending_takes_the_identifier_and_refuses_a_name(self):
        mid, err = bus.bus_send(self.con, "alice", [], "s", "b", tac_id=self.tac)
        self.assertIsNotNone(mid, err)
        self.assertEqual(
            self.con.execute("SELECT tac_id FROM messages WHERE id=?", (mid,)).fetchone()[0],
            self.tac,
        )
        self.assertEqual([m["id"] for m in bus.bus_tac_messages(self.con, self.tac)], [mid])
        refused, err = bus.bus_send(self.con, "alice", [], "s", "b", tac_id="beacon-alert")
        self.assertIsNone(refused)
        self.assertEqual(err.code, "TAC_ID_INVALID")
        blocked, err = bus.bus_send(self.con, "bob", [], "s", "b", tac_id=self.tac)
        self.assertIsNone(blocked)
        self.assertIn(f"tac '{self.tac}'", err)

    def test_a_rename_detaches_nothing(self):
        mid, _ = bus.bus_send(self.con, "alice", [], "s", "b", tac_id=self.tac)
        bus.bus_tac_create(self.con, "beacon-parent", by="alice")
        parent = [row["tac_id"] for row in bus.bus_tac_list(self.con)
                  if row["name"] == "beacon-parent"][0]
        bus.bus_tac_link(self.con, self.tac, parent, by="alice")
        ok, msg = bus.bus_tac_rename(self.con, self.tac, "doorbell", by="alice")
        self.assertTrue(ok, msg)
        self.assertEqual(
            self.con.execute("SELECT name FROM tacs WHERE tac_id=?", (self.tac,)).fetchone()[0],
            "doorbell",
        )
        self.assertEqual([m["id"] for m in bus.bus_tac_messages(self.con, self.tac)], [mid])
        self.assertEqual(bus.bus_tac_members(self.con, self.tac), ["alice", "bob"])
        self.assertEqual(len(bus.bus_tac_links(self.con, self.tac)["parents"]), 1)
        self.assertEqual(
            self.con.execute("SELECT label FROM tacs WHERE tac_id=?", (self.tac,)).fetchone()[0],
            "doorbell work",
            "the description survives a rename",
        )
        history = self.con.execute(
            "SELECT old_name, new_name, changed_by FROM tac_name_changes"
        ).fetchall()
        self.assertEqual([tuple(r) for r in history], [("beacon-alert", "doorbell", "alice")])
        # 🔴 The old name is free for another tac to take, and nothing that was sent
        #    follows it, because the messages hold the identifier.
        self.assertTrue(bus.bus_tac_create(self.con, "beacon-alert", by="alice")[0])
        other = [row["tac_id"] for row in bus.bus_tac_list(self.con)
                 if row["name"] == "beacon-alert"][0]
        self.assertNotEqual(other, self.tac)
        self.assertEqual(bus.bus_tac_messages(self.con, other), [])

    def test_a_rename_takes_the_identifier_only(self):
        for bad in ("beacon-alert", self.tac.upper(), self.tac.replace("-", "")):
            ok, msg = bus.bus_tac_rename(self.con, bad, "doorbell", by="alice")
            self.assertFalse(ok, bad)
            self.assertEqual(msg.code, "TAC_ID_INVALID")

    def test_the_refusal_codes(self):
        absent = "0b3f1f2e-8a3c-4d5e-9f10-1a2b3c4d5e6f"
        cases = {
            "TAC_NAME_TAKEN": bus.bus_tac_create(self.con, "BEACON-ALERT", by="alice"),
            "TAC_NAME_INVALID": bus.bus_tac_create(self.con, "tac​name", by="alice"),
            "TAC_NOT_FOUND": bus.bus_tac_rename(self.con, absent, "x", by="alice"),
            "TAC_ID_INVALID": bus.bus_tac_rename(self.con, "beacon-alert", "x", by="alice"),
        }
        for code, (ok, msg) in cases.items():
            self.assertFalse(ok, code)
            self.assertEqual(msg.code, code)
            self.assertEqual(msg.retry, "never")
            self.assertIn("field", msg.details)
        ok, msg = bus.bus_tac_create(self.con, "alice", by="alice")
        self.assertFalse(ok, "a tac may not take a node's name")
        self.assertEqual(msg.code, "TAC_NAME_INVALID")

    def test_a_previous_identifier_is_recorded_and_does_not_designate(self):
        # 🔴 tac_legacy_ids is what makes an old letter readable: the string in it maps
        #    to a tac. It is history, not an address — designating with it is refused.
        self.con.execute(
            "INSERT INTO tac_legacy_ids(old_id, tac_id, migrated_at) VALUES(?,?,?)",
            ("beacon-alert", self.tac, "2026-09-18T00:00:00"),
        )
        self.assertEqual(
            self.con.execute(
                "SELECT tac_id FROM tac_legacy_ids WHERE old_id=?", ("beacon-alert",)
            ).fetchone()[0],
            self.tac,
        )
        self.assertEqual(bus.tac_resolve(self.con, "beacon-alert")[1].code, "TAC_ID_INVALID")

    def test_a_key_this_interpreter_would_not_produce_is_reported(self):
        self.assertEqual(bus.bus_tac_key_mismatches(self.con), [])
        self.con.execute(
            "UPDATE tacs SET name_key='folded-by-another-version' WHERE tac_id=?", (self.tac,)
        )
        reported = bus.bus_tac_key_mismatches(self.con)
        self.assertEqual([r["tac_id"] for r in reported], [self.tac])
        self.assertEqual(reported[0]["name"], "beacon-alert")
        # 🔴 The symptom: the name finds nothing, and the same name can be created
        #    again, because the unique index holds the other key.
        from tabus.tac_store import resolve_name

        self.assertEqual(resolve_name(self.con, "beacon-alert"), [])
        self.assertTrue(bus.bus_tac_create(self.con, "beacon-alert", by="alice")[0])

    def test_the_scheme_is_reported(self):
        # 🔴 The two states serve reads and sends identically, so the only way to tell a
        #    converted ledger from one that was going to be converted is to ask.
        self.assertEqual(bus.tac_scheme(self.con), {"converted": True, "unconverted": 0})
        self.con.execute("INSERT INTO tacs(tac_id, label, created_at) "
                         "VALUES('old-one', 'from before', '2026-09-18T05:00:00')")
        # 🔴 One row on the old scheme makes the whole ledger not converted — that is what
        #    create and rename refuse on. The count says how many are left; None there means
        #    the columns are not even present.
        self.assertEqual(bus.tac_scheme(self.con), {"converted": False, "unconverted": 1})

    def test_the_listing_carries_both(self):
        listed = {row["name"]: row for row in bus.bus_tac_list(self.con)}
        self.assertEqual(listed["beacon-alert"]["tac_id"], self.tac)
        self.assertEqual(listed["beacon-alert"]["member_count"], 2)


if __name__ == "__main__":
    unittest.main()
