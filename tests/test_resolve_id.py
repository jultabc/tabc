#!/usr/bin/env python3
"""resolve_recipient_message_id unit tests: message ID prefix resolution.

Contract:
- Exact full IDs take precedence and return unchanged without scanning.
- Prefix matches: zero means absent, one succeeds, and two or more are ambiguous and rejected.
- Scope to the recipient's inbox; do not count another recipient's message IDs.
- Escape LIKE wildcards (%, _, and backslash) to prevent false matches or injection.
Temporary database only, never live data. Run directly and check the exit code."""

import os
import sys
import tempfile

os.environ["TABC_DB"] = os.path.join(tempfile.mkdtemp(prefix="resolveid_"), "t.db")
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

# erin's inbox has two messages sharing a prefix and one unique message.
AID1 = "aaaaaaaa-1111-4111-8111-111111111111"
AID2 = "aaaaaaaa-2222-4222-8222-222222222222"  # Shares the aaaaaaaa- prefix with AID1.
BID = "bbbbbbbb-3333-4333-8333-333333333333"
CID = "cccccccc-4444-4444-8444-444444444444"  # Another inbox provides a scope control.
for mid in (AID1, AID2, BID):
    tabus.bus_send(con, "alice", ["erin"], "s", "b", message_id=mid)
tabus.bus_send(con, "alice", ["other"], "s", "b", message_id=CID)
con.commit()


def R(prefix, recipient="erin"):
    return tabus.resolve_recipient_message_id(con, recipient, prefix)


# 1. Exact full ID returns unchanged without scanning.
fid, err = R(AID1)
check("1 exact full ID returns unchanged", fid == AID1 and err is None)

# 2. Unique prefix resolves to the full ID.
fid, err = R("bbbbbbbb")
check("2 unique prefix resolves to the full ID", fid == BID and err is None)

# 3. A longer unique prefix distinguishes one of the two overlapping IDs.
fid, err = R("aaaaaaaa-1")
check("3 longer prefix identifies one message", fid == AID1 and err is None)

# 4. Reject ambiguous prefixes with two or more matches: full_id is None and error indicates ambiguity.
fid, err = R("aaaaaaaa")
check("4 ambiguous prefix returns full_id None", fid is None)
check("4 ambiguity is reported in the error", bool(err) and "ambiguous" in err)
check("4 ambiguity reports two candidates", bool(err) and "2 messages" in err)

# 5. Reject absent prefixes: full_id is None and error indicates no match.
fid, err = R("zzzzzzzz")
check("5 absent prefix returns full_id None", fid is None)
check("5 error reports no match", bool(err) and "no such" in err)

# 6. Scope: CID in the other inbox cannot be resolved for erin.
fid, err = R("cccccccc", recipient="erin")
check("6 another inbox's IDs do not count", fid is None and bool(err))
fid2, err2 = R("cccccccc", recipient="other")
check("6 control: ID resolves in its own inbox", fid2 == CID and err2 is None)

# 7. Escape %: aaaaaaaa% is literal and matches nothing, not an ambiguous wildcard.
fid, err = R("aaaaaaaa%")
check("7 literal % yields no match, not ambiguity", fid is None and "no such" in (err or ""))

# 8. Escape _: underscore in aaaaaaaa_1111 must not match a dash as a wildcard.
fid, err = R("aaaaaaaa_1111")
check("8 literal _ does not incorrectly resolve to AID1", fid is None and "no such" in (err or ""))

# 9. Reject empty input with a normal error.
fid, err = R("")
check("9 empty prefix is rejected", fid is None and bool(err))
fid, err = tabus.resolve_recipient_message_id(con, "", "x")
check("9 empty recipient is rejected", fid is None and bool(err))

# 10. Reject non-string input without crashes, avoiding replace AttributeError or binding failures (Codex).
for bad in (123, ["aaaaaaaa"], {"x": 1}, 0):
    try:
        fid, err = tabus.resolve_recipient_message_id(con, "erin", bad)
        ok = fid is None and bool(err)
    except Exception as e:  # noqa: BLE001 — any exception means the input defense failed
        ok = False
        err = f"\uc608\uc678 \ub204\ucd9c: {type(e).__name__}"
    check(f"10 non-string message_id={bad!r} is rejected without crashing", ok)
try:
    fid, err = tabus.resolve_recipient_message_id(con, 999, "aaaaaaaa")
    check("10 non-string recipient is rejected without crashing", fid is None and bool(err))
except Exception as e:  # noqa: BLE001
    check(
        f"10 non-string recipient is rejected without crashing (leaked exception {type(e).__name__})",
        False,
    )

print(f"\n{'❌ FAIL' if fails else '✅ ALL PASS'} — {len(fails)} failed")
sys.exit(1 if fails else 0)
