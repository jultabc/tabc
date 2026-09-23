#!/usr/bin/env python3
"""Creating and renaming tacs on the UUID scheme (in-memory ledger).

Pins:
- create mints the identifier; a caller cannot choose it, and the name is stored exactly
  as sent (no trimming beyond surrounding spaces, no normalizing).
- Names are unique on the folded key, so look-alike spellings collide; a rename keeps its
  own name.
- label is never written by create or rename: the description survives both.
- rename takes a canonical UUID only, and records the old name.
- Both refuse while any row is still without a UUID, which is the state after a rollback.
- Lookup and search compare on the folded key, not on sqlite's lower().
"""

import os
import sqlite3
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tabus import tac_store as store  # noqa: E402
from tabus.tac_identity import is_uuid_id  # noqa: E402

NFC_CAFE = "caf\u00e9"
NFD_CAFE = "cafe\u0301"
AT = "2026-09-18T05:00:00"


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.executescript("""
            CREATE TABLE nodes(node_id TEXT PRIMARY KEY);
            CREATE TABLE tacs(tac_id TEXT PRIMARY KEY, label TEXT, created_at TEXT, created_by TEXT);
        """)
        self.con.execute("INSERT INTO nodes VALUES('codegg')")
        store.ensure_columns(self.con)

    def tearDown(self):
        self.con.close()

    def rows(self):
        return self.con.execute("SELECT tac_id, label, name, name_key FROM tacs ORDER BY name").fetchall()

    def test_create_mints_the_identifier_and_keeps_the_name_as_sent(self):
        ok, message, row = store.create(self.con, "  beacon-alert  ", by="codegg", at=AT)
        self.assertTrue(ok, message)
        self.assertTrue(is_uuid_id(row["tac_id"] if hasattr(row, "keys") else row["tac_id"]))
        self.assertEqual(row["name"], "beacon-alert")
        stored = self.rows()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0][2], "beacon-alert")
        self.assertIsNone(stored[0][1], "label is not written by create")
        ok, _, row2 = store.create(self.con, NFD_CAFE, by="codegg", at=AT)
        self.assertTrue(ok)
        kept = self.con.execute("SELECT name FROM tacs WHERE tac_id=?", (row2["tac_id"],)).fetchone()[0]
        self.assertEqual(kept, NFD_CAFE, "the name is stored in the spelling that arrived")

    def test_names_are_unique_on_the_folded_key(self):
        self.assertTrue(store.create(self.con, "beacon-alert", at=AT)[0])
        for clash in ("beacon-alert", "BEACON-ALERT", "Beacon-Alert"):
            ok, message, _ = store.create(self.con, clash, at=AT)
            self.assertFalse(ok, clash)
            self.assertIn("already taken", message)
        self.assertTrue(store.create(self.con, NFC_CAFE, at=AT)[0])
        ok, message, _ = store.create(self.con, NFD_CAFE, at=AT)
        self.assertFalse(ok, "the other normal form is the same name")
        self.assertIn("already taken", message)
        self.assertEqual(len(self.rows()), 2)

    def test_a_tac_may_not_take_a_node_name(self):
        ok, message, _ = store.create(self.con, "codegg", at=AT)
        self.assertFalse(ok)
        self.assertIn("node name", message)

    def test_refused_names_write_nothing(self):
        for bad in ("", "   ", "tac\u200bname", "tac\u3000name", 7):
            ok, _, _ = store.create(self.con, bad, at=AT)
            self.assertFalse(ok, repr(bad))
        self.assertEqual(self.rows(), [])

    def test_rename_keeps_the_label_and_records_the_old_name(self):
        _, _, row = store.create(self.con, "beacon-alert", at=AT)
        tac = row["tac_id"]
        self.con.execute("UPDATE tacs SET label=? WHERE tac_id=?", ("doorbell and alerts", tac))
        ok, message, renamed = store.rename(self.con, tac, "beacon-alert-2", by="codegg", at=AT)
        self.assertTrue(ok, message)
        stored = self.con.execute("SELECT name, label FROM tacs WHERE tac_id=?", (tac,)).fetchone()
        self.assertEqual(stored[0], "beacon-alert-2")
        self.assertEqual(stored[1], "doorbell and alerts", "the description survives a rename")
        history = self.con.execute("SELECT old_name, new_name, changed_by FROM tac_name_changes").fetchall()
        self.assertEqual(history, [("beacon-alert", "beacon-alert-2", "codegg")])

    def test_rename_takes_only_a_canonical_uuid(self):
        _, _, row = store.create(self.con, "beacon-alert", at=AT)
        tac = row["tac_id"]
        for bad in (tac.upper(), "beacon-alert", tac.replace("-", ""), "{%s}" % tac):
            ok, message, _ = store.rename(self.con, bad, "other", at=AT)
            self.assertFalse(ok, bad)
            self.assertIn("canonical UUID", message)
        ok, message, _ = store.rename(self.con, "0b3f1f2e-8a3c-4d5e-9f10-1a2b3c4d5e6f", "other", at=AT)
        self.assertFalse(ok)
        self.assertIn("no such tac", message)

    def test_rename_to_its_own_name_and_to_a_taken_name(self):
        _, _, first = store.create(self.con, "beacon-alert", at=AT)
        _, _, second = store.create(self.con, "tac-uuid", at=AT)
        ok, _, _ = store.rename(self.con, first["tac_id"], "Beacon-Alert", at=AT)
        self.assertTrue(ok, "a tac keeps its own name under a different case")
        ok, message, _ = store.rename(self.con, second["tac_id"], "beacon-alert", at=AT)
        self.assertFalse(ok)
        self.assertIn("already taken", message)

    def test_an_empty_ledger_without_the_columns_still_refuses(self):
        # 🔴 Nothing to list, so the row scan says "all converted" while the columns are
        #    missing; create used to run on and die on the INSERT (jack E1).
        con = sqlite3.connect(":memory:")
        con.executescript("CREATE TABLE nodes(node_id TEXT PRIMARY KEY);"
                          "CREATE TABLE tacs(tac_id TEXT PRIMARY KEY, label TEXT, created_at TEXT, created_by TEXT);")
        self.assertEqual(con.execute("SELECT COUNT(*) FROM tacs").fetchone()[0], 0)
        ok, message, _ = store.create(con, "beacon-alert", at=AT)
        self.assertFalse(ok)
        self.assertIn("migration", message)
        ok, message, _ = store.rename(con, "0b3f1f2e-8a3c-4d5e-9f10-1a2b3c4d5e6f", "beacon-alert", at=AT)
        self.assertFalse(ok)
        self.assertIn("migration", message)
        con.close()

    def test_both_stop_while_a_row_has_no_uuid(self):
        self.con.execute("INSERT INTO tacs(tac_id, label) VALUES('beacon-alert', 'old row')")
        self.assertEqual(store.unconverted(self.con), ["beacon-alert"])
        ok, message, _ = store.create(self.con, "new-one", at=AT)
        self.assertFalse(ok)
        self.assertIn("migration", message)
        ok, message, _ = store.rename(self.con, "0b3f1f2e-8a3c-4d5e-9f10-1a2b3c4d5e6f", "new-one", at=AT)
        self.assertFalse(ok)
        self.assertIn("migration", message)

    def test_a_row_with_a_name_but_a_legacy_id_still_counts_as_unconverted(self):
        # 🔴 The state a rollback leaves: the old release writes the name it was given as the
        #    identifier, so the row has a name and no UUID.
        self.con.execute("INSERT INTO tacs(tac_id, name, name_key) VALUES('beacon-alert','beacon-alert','beacon-alert')")
        self.assertEqual(store.unconverted(self.con), ["beacon-alert"])
        ok, message, _ = store.create(self.con, "new-one", at=AT)
        self.assertFalse(ok)
        self.assertIn("migration", message)

    def test_a_key_written_by_another_unicode_version_is_reported(self):
        # 🔴 A row written where the name folds differently carries a key this interpreter
        #    would not produce, and the lookup then misses it — measured across 3.14 and 3.9
        #    (woo). The key is planted here, so this also covers what follows once such a row
        #    exists: the unique index does not see it either. Recomputing is what shows it.
        #    On the write path the unassigned-character rule refuses the name first, so the
        #    second tac does not arise between those two versions (woo, against jack's
        #    simulation).
        _, _, row = store.create(self.con, "beacon-alert", at=AT)
        self.assertEqual(store.key_mismatches(self.con), [])
        self.con.execute("UPDATE tacs SET name_key='beacon-alert-as-another-version-folded-it' "
                         "WHERE tac_id=?", (row["tac_id"],))
        mismatched = store.key_mismatches(self.con)
        self.assertEqual([item[0] for item in mismatched], [row["tac_id"]])
        self.assertEqual(store.resolve_name(self.con, "beacon-alert"), [], "the row is invisible to a lookup")
        ok, _, second = store.create(self.con, "beacon-alert", at=AT)
        self.assertTrue(ok, "the unique index does not catch it either")
        names = [r[0] for r in self.con.execute("SELECT name FROM tacs ORDER BY tac_id")]
        self.assertEqual(sorted(names), ["beacon-alert", "beacon-alert"], "two tacs, one name")
        self.assertEqual(len(store.key_mismatches(self.con)), 1)

    def test_lookup_and_search_use_the_folded_key(self):
        _, _, row = store.create(self.con, NFC_CAFE + "-tac", at=AT)
        store.create(self.con, "beacon-alert", at=AT)
        # sqlite folds ASCII only, so these are the cases a lower() comparison would miss.
        self.assertEqual(self.con.execute("SELECT lower(?) = lower(?)",
                                          (NFC_CAFE + "-tac", (NFC_CAFE + "-tac").upper())).fetchone()[0], 0)
        found = store.resolve_name(self.con, (NFC_CAFE + "-TAC").upper())
        self.assertEqual([r[0] for r in found], [row["tac_id"]])
        self.assertEqual([r[0] for r in store.resolve_name(self.con, NFD_CAFE + "-tac")], [row["tac_id"]])
        self.assertEqual(store.resolve_name(self.con, "beacon"), [], "exact match only")
        self.assertEqual([r[1] for r in store.search_name(self.con, "BEACON")], ["beacon-alert"])
        # 🔴 The fragment carries an accent in the other case: sqlite's lower() would not match it.
        self.assertEqual([r[0] for r in store.search_name(self.con, NFC_CAFE.upper())], [row["tac_id"]])
        self.assertEqual([r[0] for r in store.search_name(self.con, NFD_CAFE)], [row["tac_id"]])
        store.create(self.con, "tac-uuid", at=AT)
        self.assertEqual(sorted(r[1] for r in store.search_name(self.con, "TAC")),
                         [NFC_CAFE + "-tac", "tac-uuid"], "a fragment matches every name that holds it")
        self.assertEqual(store.search_name(self.con, "nothing-here"), [])


if __name__ == "__main__":
    unittest.main()
