#!/usr/bin/env python3
"""Verify SHADOW observations do not suppress LIVE alarms."""

import os
import sqlite3
import sys
import tempfile
from contextlib import closing

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="doorbell_mode_test_")
os.environ["DOORBELL_LEDGER"] = os.path.join(TMP, "doorbell_ring.db")
sys.path.insert(0, ROOT)

from tabus import doorbell_poll as doorbell_poll  # noqa: E402

fails = []


def check(name, condition):
    print(f"  {'PASS' if condition else 'FAIL'}  {name}")
    if not condition:
        fails.append(name)


doorbell_poll.record_ring("bob", 42, 2, "SHADOW", "SHADOW_ONLY")
with closing(doorbell_poll.ledger()) as led:
    check(
        "SHADOW watermark is visible in SHADOW",
        doorbell_poll._last_rung_high(led, "bob", "SHADOW") == 42,
    )
    check(
        "SHADOW watermark is not visible in LIVE",
        doorbell_poll._last_rung_high(led, "bob", "LIVE") == 0,
    )

original_ro = doorbell_poll._tabus_ro
original_scan = doorbell_poll.scan_unread
doorbell_poll._tabus_ro = lambda: sqlite3.connect(":memory:")
# scan_unread returns (recipient, unread, high, ids, has_broadcast); use False because this test is unrelated to broadcasts.
doorbell_poll.scan_unread = lambda _con: [("bob", 2, 42, ["message-42"], False)]
try:
    check(
        "first LIVE after SHADOW_ONLY is eligible again",
        len(doorbell_poll.decide("LIVE")) == 1,
    )
    doorbell_poll.record_ring("bob", 42, 2, "LIVE", "SUCCESS")
    check("successful LIVE suppresses the same position", doorbell_poll.decide("LIVE") == [])
    check(
        "successful LIVE does not change the SHADOW watermark",
        doorbell_poll.decide("SHADOW") == [],
    )
finally:
    doorbell_poll._tabus_ro = original_ro
    doorbell_poll.scan_unread = original_scan

print(f"\n{'FAIL' if fails else 'ALL PASS'} — {len(fails)} failed")
sys.exit(1 if fails else 0)
