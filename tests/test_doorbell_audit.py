#!/usr/bin/env python3
"""doorbelld startup audit: record who, why, when, and mode for every restart in a temporary ledger.

Contract:
- _record_start(mode) adds a starts row with at, mode, pid, ppid, os_user, and by_who/why from the environment.
- DOORBELL_BY/DOORBELL_WHY provide attribution; without them, time, mode, user, and pid still allow partial tracing.
- Intended to trace repeated manual LIVE restarts, separate from watchdog restarts.
"""

import os
import sys
import tempfile
from contextlib import closing

TMP = tempfile.mkdtemp(prefix="dbaudit_")
os.environ["TABC_DB"] = os.path.join(
    TMP, "t.db"
)  # Required for tabus imports in doorbelld.
os.environ["DOORBELL_LEDGER"] = os.path.join(TMP, "ring.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tabus import doorbell_poll as doorbell_poll  # noqa: E402
from tabus import doorbell as doorbelld  # noqa: E402

fails = []


def check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        fails.append(name)


def latest():
    with closing(doorbell_poll.ledger()) as led:
        return led.execute(
            "SELECT at, mode, pid, ppid, os_user, by_who, why "
            "FROM starts ORDER BY id DESC LIMIT 1"
        ).fetchone()


# 1. Restart with by/why environment variables.
os.environ["DOORBELL_BY"] = "bob"
os.environ["DOORBELL_WHY"] = '\uc544\uce68 \ud14c\uc2a4\ud2b8'
doorbelld._record_start("LIVE")
r = latest()  # (at, mode, pid, ppid, os_user, by_who, why)
check("creates a starts row", r is not None)
check("records mode=LIVE", r[1] == "LIVE")
check("pid matches this process", r[2] == os.getpid())
check("records os_user", bool(r[4]))
check("by_who=bob (env)", r[5] == "bob")
check("why matches the morning-test environment value", r[6] == '\uc544\uce68 \ud14c\uc2a4\ud2b8')
check("records the at timestamp", bool(r[0]))

# 2. Without environment attribution, by/why may be None but time, mode, and user remain.
del os.environ["DOORBELL_BY"]
del os.environ["DOORBELL_WHY"]
doorbelld._record_start("SHADOW")
r2 = latest()
check("second row records mode=SHADOW", r2[1] == "SHADOW")
check("by_who is NULL without the environment variable", r2[5] is None)
check("why is NULL without the environment variable", r2[6] is None)
check("os_user remains without environment attribution", bool(r2[4]))

# 3. Two restarts accumulate two audit rows for tracing.
with closing(doorbell_poll.ledger()) as led:
    n = led.execute("SELECT COUNT(*) FROM starts").fetchone()[0]
check("two restarts accumulate two rows", n == 2)

print("\n" + ("✅ ALL PASS" if not fails else f"❌ {len(fails)} failed: {fails}"))
sys.exit(1 if fails else 0)
