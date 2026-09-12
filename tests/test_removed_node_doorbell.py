#!/usr/bin/env python3
"""Verify soft-removed nodes are excluded from alarm candidates."""

import os
import sqlite3
import sys
from contextlib import closing

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tabus import doorbell_poll as doorbell_poll  # noqa: E402

fails = []


def check(name, condition):
    print(f"  {'PASS' if condition else 'FAIL'}  {name}")
    if not condition:
        fails.append(name)


def fixture(with_removed_table):
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            tac_id TEXT,
            broadcast INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE deliveries (
            id INTEGER PRIMARY KEY,
            message_id TEXT NOT NULL,
            recipient_id TEXT NOT NULL,
            state TEXT NOT NULL
        );
        INSERT INTO messages(id, tac_id, broadcast) VALUES ('m1', NULL, 0);
        INSERT INTO deliveries(id, message_id, recipient_id, state)
        VALUES (1, 'm1', 'active', 'ACCEPTED'),
               (2, 'm1', 'removed', 'ACCEPTED');
        """)
    if with_removed_table:
        con.executescript("""
            CREATE TABLE removed_nodes (node_id TEXT PRIMARY KEY);
            INSERT INTO removed_nodes(node_id) VALUES ('removed');
            """)
    return con


with closing(fixture(True)) as con:
    rows = doorbell_poll.scan_unread(con)
    check(
        "excludes removed nodes when removed_nodes exists", [row[0] for row in rows] == ["active"]
    )

with closing(fixture(False)) as con:
    rows = doorbell_poll.scan_unread(con)
    check(
        "iteration survives missing removed_nodes during deployment skew",
        [row[0] for row in rows] == ["active", "removed"],
    )

print(f"\n{'FAIL' if fails else 'ALL PASS'} — {len(fails)} failed")
sys.exit(1 if fails else 0)
