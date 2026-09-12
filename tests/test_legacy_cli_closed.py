"""Retired commands must fail before touching the local store."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class LegacyCliClosed(unittest.TestCase):
    def test_retired_commands_leave_store_untouched(self):
        with tempfile.TemporaryDirectory() as root:
            db = Path(root) / "store.db"
            env = dict(os.environ, TABC_HOME=root, TABC_DB=str(db))
            for command in (
                "init", "register", "beat", "act", "who", "send", "pull",
                "ack", "audit", "digest", "search", "agent", "watch",
            ):
                with self.subTest(command=command):
                    result = subprocess.run(
                        [sys.executable, "-m", "tabus.bus", command],
                        env=env, capture_output=True, text=True, timeout=10,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("invalid choice", result.stderr)
                    self.assertFalse(db.exists())

    def test_key_recovery_remains_available(self):
        result = subprocess.run(
            [sys.executable, "-m", "tabus.bus", "rotate-key", "--help"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("pubkey", result.stdout)


if __name__ == "__main__":
    unittest.main()
