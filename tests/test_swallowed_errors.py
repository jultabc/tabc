"""Errors that used to be swallowed are now reported.

Each case had an except that did nothing, so a failure read the same as a clean
result: a link file nobody read counted as "0 broken", a file the credential scan
could not open counted as "0 hits", and a purge that could not delete open
records went on anyway. An unreadable host id already raised; that is pinned too.
"""

import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["TABC_DB"] = os.path.join(tempfile.mkdtemp(prefix="swallow_"), "t.db")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import tabus  # noqa: E402
from tabus import cli  # noqa: E402
import link_check  # noqa: E402


def unreadable_file(d, name, text="x"):
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(p, 0)
    return p


class LinkCheck(unittest.TestCase):
    def test_unreadable_file_is_counted_broken(self):
        if os.geteuid() == 0:
            self.skipTest("root reads any file")
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "ok.md"), "w", encoding="utf-8") as f:
                f.write("[a](ok.md)\n")
            p = unreadable_file(d, "locked.md", "[b](missing.md)\n")
            try:
                broken, _ = link_check.check([d])
            finally:
                os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
        self.assertTrue(any(path.endswith("locked.md") and url.startswith("<unreadable") for path, url in broken), broken)


class SecuritySnapshot(unittest.TestCase):
    def run_snapshot(self, d):
        r = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "security_snapshot.py"), d],
                           capture_output=True, text=True, env=dict(os.environ, TABC_HOME=d))
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_unreadable_tracked_file_is_listed(self):
        if os.geteuid() == 0:
            self.skipTest("root reads any file")
        with tempfile.TemporaryDirectory() as d:
            subprocess.run(["git", "init", "-q", d], check=True)
            p = unreadable_file(d, "conf.py", 'password = "abcdefghij"\n')
            os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
            subprocess.run(["git", "-C", d, "add", "conf.py"], check=True)
            os.chmod(p, 0)
            try:
                out = self.run_snapshot(d)
            finally:
                os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
        self.assertEqual([u["file"] for u in out["unreadable_files"]], ["conf.py"])
        self.assertIsNone(out["scan_error"])

    def test_missing_git_list_is_not_clean(self):
        with tempfile.TemporaryDirectory() as d:
            r = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "security_snapshot.py"), d],
                               capture_output=True, text=True,
                               env=dict(os.environ, TABC_HOME=d, PATH="/nonexistent"))
        out = json.loads(r.stdout)
        self.assertEqual(out["tracked_cred_literal_hits"], 0)
        self.assertIn("git ls-files failed", out["scan_error"] or "")


class PurgeStopsOnDatabaseError(unittest.TestCase):
    def test_locked_open_records_stop_the_purge(self):
        con = tabus.connect()
        con.executescript(tabus.SCHEMA)
        con.commit()
        tabus.bus_register(con, "zed", "generic")
        con.commit()

        class Locked:
            """Passes everything through, except the open-records delete."""

            def __init__(self, inner):
                self.inner = inner

            def execute(self, sql, *a):
                if sql.startswith("DELETE FROM mailbox_opens"):
                    raise sqlite3.OperationalError("database is locked")
                return self.inner.execute(sql, *a)

            def __getattr__(self, name):
                return getattr(self.inner, name)

        with self.assertRaises(sqlite3.OperationalError):
            tabus.bus_remove(Locked(con), "zed", purge=True)
        self.assertIsNotNone(con.execute("SELECT 1 FROM nodes WHERE node_id='zed'").fetchone())
        con.close()

    def test_missing_table_is_still_harmless(self):
        con = tabus.connect()
        con.executescript(tabus.SCHEMA)
        con.commit()
        tabus.bus_register(con, "yan", "generic")
        con.commit()
        con.execute("DROP TABLE IF EXISTS mailbox_opens")
        ok, msg = tabus.bus_remove(con, "yan", purge=True)
        self.assertTrue(ok, msg)
        con.close()


class HostId(unittest.TestCase):
    """cli._host_id catches OSError on read, but an existing file then fails the
    exclusive create and the second read raises. Pinned so a later change cannot
    turn it into a silent new id."""

    def test_unreadable_host_id_is_not_replaced_silently(self):
        old = os.environ.get("TABC_HOME")
        with tempfile.TemporaryDirectory() as d:
            os.environ["TABC_HOME"] = d
            try:
                os.mkdir(os.path.join(d, "host_id"))  # exists, but open() fails with IsADirectoryError
                with self.assertRaises(OSError):
                    cli._host_id()
            finally:
                if old is None:
                    os.environ.pop("TABC_HOME", None)
                else:
                    os.environ["TABC_HOME"] = old

    def test_missing_host_id_is_created(self):
        old = os.environ.get("TABC_HOME")
        with tempfile.TemporaryDirectory() as d:
            os.environ["TABC_HOME"] = d
            try:
                hid = cli._host_id()
                self.assertTrue(hid)
                self.assertEqual(cli._host_id(), hid)
            finally:
                if old is None:
                    os.environ.pop("TABC_HOME", None)
                else:
                    os.environ["TABC_HOME"] = old


if __name__ == "__main__":
    unittest.main()
