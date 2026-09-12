#!/usr/bin/env python3
"""doorbell_poll — hang the doorbell on state instead of on an event.

Why this exists, from measurement:
    The doorbell used to ring only from the send path — once, at the moment of
    sending. If no session was listening right then, that ring was gone forever.
    The message stayed in the ledger, but there was no path that would ever
    mention it again.
    That is the structural cause behind 232 unread messages, the oldest of them
    54.7 hours old.

    This module inverts it and reads ledger state instead. If unread mail is
    still there, it rings. It rings with no sender present, and missing one round
    just means the next round catches it.

🔴 The goal is moving from an event trigger to a state trigger. The Postgres
    notification dependency once lived in a single ring function; now that this
    state trigger runs, that function has been removed — tabus's only transport
    is the file spool.

Boundaries, deliberately not crossed:
    - Injection into a terminal does not happen here. There is exactly one place
      that injects, and duplicating it would create a second authority. This
      module decides what to ring and hands the actual delivery to its caller.
    - The message store is opened read-only. One writer, and it is not this.
      This module's own records go in a separate file.
"""

import os
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# 🔴 "Unread" is defined in one place, and this imports it rather than restating
#   it. While it was a hand-copied duplicate, adding a state on one side left the
#   doorbell quietly counting by the old definition — one meaning written by hand
#   in two places, which is the recurring shape.
#   The sys.path insert this used to need is gone: inside the package a relative
#   import finds its sibling without touching the interpreter's search path.
from . import paths
from .bus import UNREAD_STATES, parse_instant

TABC_DB = os.environ.get("TABC_DB", paths.state("tabus.db"))
LEDGER_DB = os.environ.get(
    "DOORBELL_LEDGER", os.path.join(paths.home(), "data", "doorbell_ring.db")
)

# 🔴 Outcomes that count as having rung. Only these suppress a re-ring.
#   This was once a list of exceptions instead — everything except UNKNOWN
#   counted as rung. So FAILED and NOT_APPLICABLE also raised the watermark, and
#   every message below it stopped being a candidate forever. One real message
#   was buried exactly that way: it failed to deliver, and the failure itself
#   permanently blocked any retry.
#   🔴 An exception list makes every new value default to the dangerous side.
#   Adding one more outcome would have reproduced the same bug silently.
#   Inverting it means an unrecognized outcome defaults to non-suppressing:
#   failing to ring is not the same as having rung.
#   The legacy entry covers rings that went out through the old path. Those did
#   ring, so they suppress. It has its own name so it can be counted; when it
#   reaches zero, that path can be deleted.
SUPPRESSING = ("SUCCESS", "SUCCESS_LEGACY", "SHADOW_ONLY", "TEXT_ONLY")
UNKNOWN_RETRY_SECONDS = 30
_unknown_attempts = {}

LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS observations (
    recipient     TEXT NOT NULL,
    message_id    TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    PRIMARY KEY (recipient, message_id)
);
CREATE TABLE IF NOT EXISTS rings (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient TEXT NOT NULL,
    high_id   INTEGER NOT NULL,
    unread    INTEGER NOT NULL,
    mode      TEXT NOT NULL,
    outcome   TEXT,
    at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rings_sup ON rings(recipient, outcome, high_id);
-- 🔴 Evidence that a round ran. One row every round, even with nothing to ring.
--   Without it, silence has two meanings: running fine with nothing to do, or
--   dead. That happened — eleven and a half hours with zero records, and no way
--   to tell which.
--   The design note said "leave evidence even for an empty result" and the code
--   did not do it.
--   status: OK for a completed round, ERROR for one that raised — which still
--   leaves its trace.
CREATE TABLE IF NOT EXISTS rounds (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at         TEXT NOT NULL,
    mode       TEXT NOT NULL,
    status     TEXT NOT NULL,
    recipients INTEGER,
    unread     INTEGER,
    would_ring INTEGER,
    err        TEXT
);
CREATE INDEX IF NOT EXISTS idx_rounds_at ON rounds(at);
"""


def _now():
    return datetime.now(ZoneInfo("Asia/Seoul")).isoformat(timespec="seconds")


def ledger():
    """This module's own ledger, separate from the message store. That store keeps
    exactly one writer, and it is not this."""
    os.makedirs(os.path.dirname(os.path.abspath(LEDGER_DB)), exist_ok=True)
    con = sqlite3.connect(LEDGER_DB, timeout=5.0)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(LEDGER_DDL)
    return con


def _tabus_ro():
    """🔴 Open read-only. Enforced through the connection URI so that even a
    mistake cannot write to the message store."""
    path = os.path.abspath(TABC_DB)
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    con.row_factory = sqlite3.Row
    return con


def scan_unread(bcn):
    """Unread mail per recipient.

    Returns [(recipient, unread, high_id, [message_id...], has_broadcast)].

    has_broadcast is whether any of that recipient's unread is an announcement,
    which is what lets a broadcast ring through an otherwise silent mode.
    """
    q = ",".join("?" * len(UNREAD_STATES))
    # 🔴 Removed nodes drop out of the candidate set here too. Removal promises the
    #    doorbell stops, and without this filter old unread deliveries keep the node
    #    cycling through failed ring attempts forever.
    #    Code and schema deploy separately, so this checks whether the table exists.
    #    On a database that does not have it yet, only the filter is inactive and
    #    the doorbell keeps running.
    has_removed = (
        bcn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='removed_nodes'"
        ).fetchone()
        is not None
    )
    removed_filter = (
        "AND d.recipient_id NOT IN (SELECT node_id FROM removed_nodes)"
        if has_removed
        else ""
    )
    # Both direct and TAC deliveries are notification candidates.
    # 🔴 The broadcast column may not exist yet: the migration and this process
    #    restart independently, and there is a window between them. Missing, it
    #    reads as 0 — the doorbell keeps running and only the broadcast override
    #    stays inactive until the column arrives.
    has_bc = any(
        r["name"] == "broadcast" for r in bcn.execute("PRAGMA table_info(messages)")
    )
    bc_sel = "m.broadcast" if has_bc else "0"
    # 🔴 Quiet is one concept: a node whose snooze has not expired drops out of the
    #    candidate set entirely, and not even a broadcast gets through.
    #    It returns on its own, because the comparison is re-evaluated every poll.
    #    Compared through parse_instant — never as strings.
    #    Missing column yields an empty set rather than an exception.
    has_snz = any(
        r["name"] == "snooze_until" for r in bcn.execute("PRAGMA table_info(nodes)")
    )
    snoozed = set()
    if has_snz:
        _now_utc = datetime.now(timezone.utc)
        for r in bcn.execute(
            "SELECT node_id, snooze_until FROM nodes WHERE snooze_until IS NOT NULL"
        ):
            try:
                if parse_instant(r["snooze_until"]) > _now_utc:
                    snoozed.add(r["node_id"])
            except ValueError:
                pass
    rows = bcn.execute(
        f"SELECT d.recipient_id AS recipient_id, d.message_id AS message_id, d.id AS id, "
        f"{bc_sel} AS broadcast "
        f"FROM deliveries d JOIN messages m ON m.id = d.message_id "
        f"WHERE d.state IN ({q}) {removed_filter} "
        f"ORDER BY d.recipient_id, d.id",
        UNREAD_STATES,
    ).fetchall()
    grouped = {}
    for r in rows:
        g = grouped.setdefault(r["recipient_id"], {"ids": [], "high": 0, "bc": 0})
        g["ids"].append(r["message_id"])
        g["high"] = max(g["high"], r["id"])
        g["bc"] = max(g["bc"], r["broadcast"] or 0)
    return [
        (k, len(v["ids"]), v["high"], v["ids"], bool(v["bc"]))
        for k, v in sorted(grouped.items())
        # 🔴 A node's own snooze is absolute; a broadcast does not override it.
        #    Reach is guaranteed by delivery, not by ringing — a silent node comes
        #    back and pulls. Ringing and delivering are different things.
        #    A global quiet mode is where broadcast punches through, and only for
        #    nodes that have not silenced themselves.
        if k not in snoozed
    ]


def _last_rung_high(led, recipient, mode):
    """The high-water mark of what has actually rung. Only suppressing outcomes count.

    What this number means, exactly: everything up to here definitely reached
    someone. Rounds that failed to ring are not counted, so if the path opens up
    later, the messages below the mark become candidates again.
    An unknown outcome does not count either — not knowing whether it arrived is
    not the same as it having arrived.

    🔴 Modes are counted on their own axis. An observation-only watermark proves
    observation, not delivery. Merging the two would mean a message first seen in
    observation mode never rings again after the switch to live.
    """
    q = ",".join("?" * len(SUPPRESSING))
    row = led.execute(
        f"SELECT coalesce(max(high_id), 0) FROM rings "
        f"WHERE recipient=? AND mode=? "
        f"AND outcome IN ({q})",
        (recipient, mode, *SUPPRESSING),
    ).fetchone()
    return row[0]


def _write_round(led, mode, status, recipients, unread, would_ring, err=None):
    """One row per round. 🔴 It does not commit — the caller binds it into the same
    transaction as the observations.

    Only in one transaction does "round OK" also mean "the observations landed".
    Committed separately, the two can disagree.
    """
    led.execute(
        "INSERT INTO rounds(at, mode, status, recipients, unread, would_ring, err)"
        " VALUES (?,?,?,?,?,?,?)",
        (_now(), mode, status, recipients, unread, would_ring, err),
    )


def decide(mode="SHADOW"):
    """One round: record observations, and return only what should ring.

    🔴 Observations and rings are written separately.
       Merged, "seen but not rung" and "never seen at all" become the same row,
       which is the failure this module exists to avoid. Cross-checks read the
       observation side; the ring side has different counts because of suppression.

    🔴 A row goes into rounds even when there is nothing to ring.
       It belongs here rather than in the caller because there are two callers.
       In the callers it would be two authorities, and fixing one would leave the
       other behind.

    🔴🔴 What this table does and does not prove — worth stating so the next reader
       does not overreach:
       It removes the ambiguity of silence in a round that ran. An empty result now
       leaves a row saying so.
       It does not cover a round that never ran. If the process dies, no row
       appears at all, so "no row" still has two meanings: nothing was scheduled,
       or something was scheduled and did not run.
       ⚠️ Do not compute uptime or coverage from this table. The denominator would
         be the table itself, which always yields 100%. A real denominator has to
         come from an independent schedule definition, and that does not exist yet.
       This is evidence of rounds that ran, not a ledger of rounds that should have.
    """
    out = []
    recipients = 0
    total_unread = 0
    try:
        # A sqlite3 connection used as a context manager commits or rolls back but
        # does not close. At a two-second poll that would leak a connection per
        # round, so closing is applied explicitly.
        with closing(_tabus_ro()) as bcn, closing(ledger()) as led:
            for recipient, unread, high, mids, has_broadcast in scan_unread(bcn):
                recipients += 1
                total_unread += unread
                now = _now()
                led.executemany(
                    "INSERT OR IGNORE INTO observations(recipient, message_id, first_seen_at)"
                    " VALUES (?,?,?)",
                    [(recipient, m, now) for m in mids],
                )
                attempted = _unknown_attempts.get((recipient, mode))
                cooling = attempted is not None and time.monotonic() - attempted < UNKNOWN_RETRY_SECONDS
                if high > _last_rung_high(led, recipient, mode) and not cooling:
                    out.append(
                        {
                            "recipient": recipient,
                            "unread": unread,
                            "high_id": high,
                            "has_broadcast": has_broadcast,
                        }
                    )
            _write_round(led, mode, "OK", recipients, total_unread, len(out))
            led.commit()
    except Exception as e:
        # 🔴 A failed round still leaves a trace, or dead and quiet look alike again.
        #   This path is reached when the message store could not be opened, so the
        #   connection above is unusable; this writes and commits on its own.
        try:
            with closing(ledger()) as led2:
                _write_round(
                    led2, mode, "ERROR", None, None, None, f"{type(e).__name__}: {e}"
                )
                led2.commit()
        except Exception:
            pass  # If even the ledger is unwritable there is nothing left to do
            # here. The original exception propagates.
        raise
    return out


def record_ring(recipient, high_id, unread, mode, outcome):
    """Record the outcome of a ring. This is where the caller reports what happened.

    🔴 The same (recipient, high_id, mode, outcome) is written once.
       This became necessary when failing outcomes stopped suppressing: that
       recipient becomes a candidate again every round, and without a guard the
       identical row accumulates once per poll — tens of thousands of rows a day
       for a single unreachable recipient. One fact, one row.
       🔴 A changed outcome is a different tuple and does write, so transitions are
       preserved. Successes suppress anyway and never reach this guard.
       When something was first seen is answered by the observations table, not
       by this one.

    🔴 Returns True if a row was written, False if it was a duplicate and skipped.
       The caller uses that to suppress its output too. Guarding only the ledger
       and not the print left the same line repeating every two seconds, growing
       the log by 309KB an hour.
       One fact, one row, one line.
    """
    if outcome == "UNKNOWN":
        _unknown_attempts[(recipient, mode)] = time.monotonic()
    else:
        _unknown_attempts.pop((recipient, mode), None)
    with closing(ledger()) as led:
        dup = led.execute(
            "SELECT 1 FROM rings WHERE recipient=? AND high_id=? AND mode=? AND outcome=?"
            " LIMIT 1",
            (recipient, high_id, mode, outcome),
        ).fetchone()
        if dup:
            return False
        led.execute(
            "INSERT INTO rings(recipient, high_id, unread, mode, outcome, at)"
            " VALUES (?,?,?,?,?,?)",
            (recipient, high_id, unread, mode, outcome, _now()),
        )
        led.commit()
        return True


def main():
    """Standalone run: observation only. Nothing is injected anywhere."""
    interval = float(os.environ.get("DOORBELL_POLL_SEC", "2"))
    print(f"[doorbell] observation mode — store={os.path.abspath(TABC_DB)}", flush=True)
    print(f"[doorbell] ledger={os.path.abspath(LEDGER_DB)} interval={interval}s", flush=True)
    while True:
        try:
            for d in decide("SHADOW"):
                record_ring(
                    d["recipient"], d["high_id"], d["unread"], "SHADOW", "SHADOW_ONLY"
                )
                print(
                    f"  [doorbell] SHADOW would-ring {d['recipient']}"
                    f" unread={d['unread']} high={d['high_id']}",
                    flush=True,
                )
        except Exception as e:  # an observer must never take down the path it watches
            print(f"  [doorbell] round failed {type(e).__name__}: {e}", flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    main()
