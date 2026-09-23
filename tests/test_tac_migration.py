#!/usr/bin/env python3
"""Converting tac identifiers to UUIDs on a copy of a ledger.

Pins:
- The source is opened read-only and is byte-for-byte unchanged after a conversion.
- The name of a converted tac is its old string id; label is not written.
- Every reference (members, messages, links both ways) moves with the tac, and the old
  string id is kept in tac_legacy_ids.
- The preflight refuses, writing nothing, on: a tac column this tool does not rewrite,
  a reference to a tac that is gone, two tacs that would share one name, a name that
  breaks the name rules.
- Running the conversion twice is the same as running it once; a ledger with a row left
  from an older release converts that row too.
- The output file is never an existing file, and a failure publishes nothing.
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tabus import tac_migration as mig  # noqa: E402
from tabus.tac_identity import is_uuid_id  # noqa: E402

AT = "2026-09-18T06:00:00"
SCHEMA = """
CREATE TABLE tacs(tac_id TEXT PRIMARY KEY, label TEXT, created_at TEXT, created_by TEXT,
                  closed_at TEXT, close_summary TEXT, closed_by TEXT);
CREATE TABLE tac_members(tac_id TEXT NOT NULL REFERENCES tacs(tac_id), member_node_id TEXT NOT NULL,
                         PRIMARY KEY (tac_id, member_node_id));
CREATE TABLE messages(id TEXT PRIMARY KEY, tac_id TEXT, body TEXT);
CREATE TABLE tac_links(child_tac TEXT NOT NULL, parent_tac TEXT NOT NULL,
                       PRIMARY KEY (child_tac, parent_tac));
"""


def ledger(path, tacs=(("beacon-alert", "doorbell and alerts"), ("tabc-read-contract", "read contract")),
           extra=""):
    con = sqlite3.connect(path)
    con.executescript(SCHEMA + extra)
    for tac_id, label in tacs:
        con.execute("INSERT INTO tacs(tac_id, label, created_at) VALUES(?,?,?)", (tac_id, label, AT))
        con.execute("INSERT INTO tac_members VALUES(?,?)", (tac_id, "codegg"))
        con.execute("INSERT INTO messages VALUES(?,?,?)", ("m-" + tac_id, tac_id, "hello"))
    if len(tacs) > 1:
        con.execute("INSERT INTO tac_links VALUES(?,?)", (tacs[0][0], tacs[1][0]))
    con.commit()
    con.close()


class MigrationTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="tac_migration_")
        self.source = os.path.join(self.dir, "ledger.db")
        self.output = os.path.join(self.dir, "converted.db")

    def rows(self, path, query, args=()):
        con = sqlite3.connect(path)
        try:
            return con.execute(query, args).fetchall()
        finally:
            con.close()

    def convert(self, path=None):
        con = sqlite3.connect(path or self.source)
        try:
            return mig.convert(con, AT)
        finally:
            con.close()

    def test_conversion_keeps_names_labels_and_references(self):
        ledger(self.source)
        before = open(self.source, "rb").read()
        copy = os.path.join(self.dir, "copy.db")
        with open(copy, "wb") as f:
            f.write(before)
        mapping = self.convert(copy)
        self.assertEqual(sorted(item["old_id"] for item in mapping), ["beacon-alert", "tabc-read-contract"])
        for item in mapping:
            self.assertTrue(is_uuid_id(item["tac_id"]), item)
            self.assertEqual(item["name"], item["old_id"], "the name is the string the team already types")
        self.assertEqual(open(self.source, "rb").read(), before, "the source is untouched")
        names = self.rows(copy, "SELECT name, label FROM tacs ORDER BY name")
        self.assertEqual(names, [("beacon-alert", "doorbell and alerts"),
                                 ("tabc-read-contract", "read contract")], "labels survive")
        for table, column in mig.REFERENCES:
            left = self.rows(copy, f"SELECT COUNT(*) FROM {table} WHERE {column} NOT IN (SELECT tac_id FROM tacs)")
            self.assertEqual(left[0][0], 0, f"{table}.{column} moved with the tac")
        legacy = self.rows(copy, "SELECT old_id, tac_id FROM tac_legacy_ids ORDER BY old_id")
        self.assertEqual([row[0] for row in legacy], ["beacon-alert", "tabc-read-contract"])
        self.assertTrue(all(is_uuid_id(row[1]) for row in legacy))

    def test_running_it_twice_changes_nothing_the_second_time(self):
        ledger(self.source)
        first = self.convert()
        after_first = self.rows(self.source, "SELECT tac_id, name, label FROM tacs ORDER BY name")
        second = self.convert()
        self.assertEqual(second, [], "nothing is left to convert")
        self.assertEqual(self.rows(self.source, "SELECT tac_id, name, label FROM tacs ORDER BY name"), after_first)
        self.assertEqual(len(self.rows(self.source, "SELECT * FROM tac_legacy_ids")), len(first))

    def test_a_row_written_after_a_rollback_is_converted_too(self):
        # 🔴 The old release writes the name it was given as the identifier. A ledger that
        #    went back and forth holds both kinds of row; the conversion takes the old one.
        ledger(self.source)
        self.convert()
        con = sqlite3.connect(self.source)
        con.execute("INSERT INTO tacs(tac_id, label, created_at) VALUES('made-after-rollback','desc',?)", (AT,))
        con.commit()
        con.close()
        mapping = self.convert()
        self.assertEqual([item["old_id"] for item in mapping], ["made-after-rollback"])
        left = self.rows(self.source, "SELECT tac_id FROM tacs WHERE name IS NULL")
        self.assertEqual(left, [])
        self.assertEqual(self.rows(self.source, "SELECT label FROM tacs WHERE name='made-after-rollback'"),
                         [("desc",)])

    def test_preflight_refuses_and_writes_nothing(self):
        cases = {
            "unknown tac column": ("CREATE TABLE odd(prev_tac_id TEXT);", None),
            "orphan reference": (None, "INSERT INTO messages VALUES('m-x','gone','hi')"),
            "duplicate name": (None, "INSERT INTO tacs(tac_id,label,created_at) VALUES('BEACON-ALERT','x','t')"),
            "name breaks the rules": (None, "INSERT INTO tacs(tac_id,label,created_at) VALUES('bad\u200bname','x','t')"),
        }
        for label, (extra, statement) in cases.items():
            path = os.path.join(self.dir, f"case-{abs(hash(label))}.db")
            ledger(path, extra=extra or "")
            if statement:
                con = sqlite3.connect(path)
                con.execute(statement)
                con.commit()
                con.close()
            before = open(path, "rb").read()
            con = sqlite3.connect(path)
            try:
                with self.assertRaises(mig.MigrationBlocked, msg=label) as caught:
                    mig.convert(con, AT)
                self.assertTrue(caught.exception.issues, label)
            finally:
                con.close()
            self.assertEqual(open(path, "rb").read(), before, f"{label}: nothing was written")

    def test_cli_reports_without_writing_and_publishes_once(self):
        ledger(self.source)
        env = dict(os.environ, PYTHONPATH=ROOT, PYTHONDONTWRITEBYTECODE="1")
        before = open(self.source, "rb").read()
        preflight = subprocess.run([sys.executable, "-m", "tabus.tac_migration", self.source],
                                   capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(preflight.returncode, 0, preflight.stderr)
        self.assertEqual(len(json.loads(preflight.stdout)["would_convert"]), 2)
        self.assertEqual(open(self.source, "rb").read(), before, "a preflight writes nothing")
        run = subprocess.run([sys.executable, "-m", "tabus.tac_migration", self.source, "--output", self.output],
                             capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(len(json.loads(run.stdout)["converted"]), 2)
        self.assertEqual(open(self.source, "rb").read(), before, "the source is still untouched")
        self.assertTrue(all(is_uuid_id(row[0]) for row in self.rows(self.output, "SELECT tac_id FROM tacs")))
        again = subprocess.run([sys.executable, "-m", "tabus.tac_migration", self.source, "--output", self.output],
                               capture_output=True, text=True, env=env, timeout=60)
        self.assertNotEqual(again.returncode, 0, "an existing output file is never replaced")

    def test_a_key_from_another_unicode_version_blocks_the_conversion(self):
        # 🔴 The ledger already holds converted rows, one of them written where the name folds
        #    differently. Converting on top of that would carry the hidden row forward (jack).
        ledger(self.source)
        self.convert()
        con = sqlite3.connect(self.source)
        con.execute("UPDATE tacs SET name_key='folded-elsewhere' WHERE name='beacon-alert'")
        con.execute("INSERT INTO tacs(tac_id, label, created_at) VALUES('added-later','desc',?)", (AT,))
        con.commit()
        con.close()
        before = open(self.source, "rb").read()
        con = sqlite3.connect(self.source)
        try:
            with self.assertRaises(mig.MigrationBlocked) as caught:
                mig.convert(con, AT)
        finally:
            con.close()
        self.assertIn("another Unicode version", json.dumps(caught.exception.issues))
        self.assertEqual(open(self.source, "rb").read(), before, "nothing was written")

    def test_a_name_that_cannot_be_used_has_a_way_through(self):
        # 🔴 Without this the whole conversion stops on one row and there is no way out:
        #    renaming needs a UUID, and the UUID only exists after the conversion (jack N1).
        ledger(self.source, tacs=(("beacon-alert", "alerts"), ("bad\u200bname", "odd one")))
        con = sqlite3.connect(self.source)
        try:
            with self.assertRaises(mig.MigrationBlocked) as caught:
                mig.convert(con, AT)
            self.assertIn("--rename", json.dumps(caught.exception.issues), "the refusal says what to do")
            mapping = mig.convert(con, AT, {"bad\u200bname": "odd-one"})
        finally:
            con.close()
        self.assertEqual(sorted(item["name"] for item in mapping), ["beacon-alert", "odd-one"])
        self.assertEqual(self.rows(self.source, "SELECT COUNT(*) FROM tacs WHERE name IS NULL"), [(0,)])

    def test_the_output_records_what_the_source_held(self):
        # 🔴 The copy is taken at a moment; without that record nobody can measure the window
        #    between the copy and a later swap, or show the copy matched the source (jack N2).
        ledger(self.source)
        env = dict(os.environ, PYTHONPATH=ROOT, PYTHONDONTWRITEBYTECODE="1")
        run = subprocess.run([sys.executable, "-m", "tabus.tac_migration", self.source, "--output", self.output],
                             capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        state = json.loads(run.stdout)["source"]
        self.assertEqual(state["rows"]["tacs"], 2)
        self.assertEqual(state["rows"]["messages"], 2)
        self.assertEqual(state["rows"]["tac_members"], 2)
        preflight = subprocess.run([sys.executable, "-m", "tabus.tac_migration", self.source],
                                   capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(json.loads(preflight.stdout)["source"], state, "a preflight reports the same state")

    def test_the_source_is_opened_read_only(self):
        # 🔴 The source connection is only ever read from, so nothing else in this file
        #    would notice if it were opened read-write. This pins how it is opened.
        ledger(self.source)
        opened = []
        real = mig.sqlite3.connect

        def recording(target, *args, **kwargs):
            opened.append((str(target), kwargs.get("uri", False)))
            return real(target, *args, **kwargs)

        mig.sqlite3.connect = recording
        try:
            mig.main([self.source, "--output", self.output])
        finally:
            mig.sqlite3.connect = real
        source_opens = [(target, uri) for target, uri in opened if target.startswith("file:")
                        or target == self.source]
        self.assertTrue(source_opens, opened)
        for target, uri in source_opens:
            self.assertTrue(uri, "the source is opened through a URI")
            self.assertIn("mode=ro", target)

    def test_a_blocked_run_publishes_no_output_file(self):
        ledger(self.source, extra="CREATE TABLE odd(prev_tac_id TEXT);")
        env = dict(os.environ, PYTHONPATH=ROOT, PYTHONDONTWRITEBYTECODE="1")
        run = subprocess.run([sys.executable, "-m", "tabus.tac_migration", self.source, "--output", self.output],
                             capture_output=True, text=True, env=env, timeout=60)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("prev_tac_id", run.stderr)
        self.assertFalse(Path(self.output).exists(), "a blocked run leaves no file behind")


if __name__ == "__main__":
    unittest.main()
