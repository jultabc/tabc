"""Notification failures never cause an immediate second text injection."""
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tabus import doorbell, doorbell_poll as poll


class RetryTests(unittest.TestCase):
    def test_enter_failure_preserves_text_outcome(self):
        ok = types.SimpleNamespace(returncode=0, stdout="", stderr="")
        bad = types.SimpleNamespace(returncode=1, stdout="", stderr="error")
        for failure in (bad, TimeoutError()):
            with patch.object(doorbell.subprocess, "run", side_effect=[ok, failure]) as run:
                self.assertEqual(doorbell.send_to_tmux_pane("/test\t%1", "bell", True),
                                 ("TEXT_ONLY", 1))
                self.assertEqual(run.call_count, 2)
        with patch.object(doorbell.subprocess, "run", return_value=types.SimpleNamespace(returncode=0, stdout="-2")):
            self.assertEqual(doorbell.send_to_iterm_session("guid", "bell", True), ("TEXT_ONLY", 1))

    def test_unknown_cooldown_and_partial_suppression(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(poll, "LEDGER_DB", os.path.join(tmp, "rings.db")), \
                patch.object(poll, "_unknown_attempts", {}), \
                patch.object(poll, "_tabus_ro", side_effect=lambda: sqlite3.connect(":memory:")), \
                patch.object(poll, "scan_unread", return_value=[("bob", 1, 9, ["m"], False)]), \
                patch.object(poll.time, "monotonic", return_value=100) as clock:
            poll.record_ring("bob", 9, 1, "LIVE", "UNKNOWN")
            self.assertEqual(poll.decide("LIVE"), [])
            clock.return_value = 131
            self.assertEqual(len(poll.decide("LIVE")), 1)
            poll.record_ring("bob", 9, 1, "LIVE", "UNKNOWN")
            self.assertEqual(poll.decide("LIVE"), [])
            clock.return_value = 162
            self.assertEqual(len(poll.decide("LIVE")), 1)
            poll.record_ring("bob", 9, 1, "LIVE", "TEXT_ONLY")
            self.assertEqual(poll.decide("LIVE"), [])
            self.assertEqual(len(poll.decide("SHADOW")), 1)


if __name__ == "__main__":
    unittest.main()
