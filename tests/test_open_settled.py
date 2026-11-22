#!/usr/bin/env python3
"""open_settled unit tests: showing a message already marked READ or PROCESSED.

Contract:
- READ and PROCESSED deliveries return the body and their state.
- Nothing changes: delivery state, updated_at and row counts stay as they were.
- Unread, DEAD and QUARANTINED deliveries return no row, but return the state,
  so the caller can say why nothing was shown instead of "no such delivery".
- A body whose hash no longer matches is not shown (state TAMPERED).
- Another node's delivery is never returned.
Temporary database only, never live data. Run directly and check the exit code."""

import os
import sys
import tempfile

os.environ["TABC_DB"] = os.path.join(tempfile.mkdtemp(prefix="opensettled_"), "t.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tabus  # noqa: E402

fails = []


def check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        fails.append(name)


con = tabus.connect()
con.executescript(tabus.SCHEMA)
con.commit()
for n in ["alice", "erin", "other"]:
    tabus.bus_register(con, n, "generic")

ids = {
    "read": "11111111-1111-4111-8111-111111111111",
    "processed": "22222222-2222-4222-8222-222222222222",
    "accepted": "33333333-3333-4333-8333-333333333333",
    "dead": "44444444-4444-4444-8444-444444444444",
    "tampered": "55555555-5555-4555-8555-555555555555",
}
for key, mid in ids.items():
    tabus.bus_send(con, "alice", ["erin", "other"], f"s-{key}", f"body-{key}", message_id=mid)
con.commit()


def set_state(mid, state, node="erin"):
    con.execute(
        "UPDATE deliveries SET state=?, updated_at='2026-01-01T00:00:00+00:00' "
        "WHERE message_id=? AND recipient_id=?",
        (state, mid, node),
    )
    con.commit()


set_state(ids["read"], "READ")
set_state(ids["processed"], "PROCESSED")
set_state(ids["dead"], "DEAD")
set_state(ids["tampered"], "READ")
con.execute("UPDATE messages SET body='changed' WHERE id=?", (ids["tampered"],))
con.commit()


def snapshot():
    return con.execute(
        "SELECT message_id, recipient_id, state, updated_at, attempts FROM deliveries "
        "ORDER BY message_id, recipient_id"
    ).fetchall()


before = [tuple(r) for r in snapshot()]
n_before = con.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]

row, st = tabus.open_settled(con, "erin", ids["read"])
check("1 READ returns the body", row is not None and row["body"] == "body-read")
check("1 READ returns its state", st == "READ" and row["state"] == "READ")
check("1 READ returns sender and subject", row["sender_id"] == "alice" and row["subject"] == "s-read")

row, st = tabus.open_settled(con, "erin", ids["processed"])
check("2 PROCESSED returns the body", row is not None and row["body"] == "body-processed" and st == "PROCESSED")

row, st = tabus.open_settled(con, "erin", ids["accepted"])
check("3 unread returns no row and its state", row is None and st == "ACCEPTED")

row, st = tabus.open_settled(con, "erin", ids["dead"])
check("4 DEAD returns no row and DEAD", row is None and st == "DEAD")

row, st = tabus.open_settled(con, "erin", ids["tampered"])
check("5 tampered body is not shown", row is None and st == "TAMPERED")

# other still has the read message unread (ACCEPTED): erin's READ is not other's.
row, st = tabus.open_settled(con, "other", ids["read"])
check("6 another node's READ is not returned", row is None and st == "ACCEPTED")

row, st = tabus.open_settled(con, "erin", "99999999-9999-4999-8999-999999999999")
check("7 unknown id returns nothing", row is None and st is None)

after = [tuple(r) for r in snapshot()]
n_after = con.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]
check("8 nothing changed: states, updated_at, attempts", before == after)
check("8 nothing changed: row count", n_before == n_after)

if fails:
    print(f"FAIL {len(fails)}")
    sys.exit(1)
print("all passed")
