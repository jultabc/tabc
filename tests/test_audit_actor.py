#!/usr/bin/env python3
"""Audit actor default — created_by/added_by records the node, not the owner email.

exi found the tac audit actor defaulting to the owner email (layer 3), which
launders away which node acted (layer 1/2). This pins the chain: explicit --by,
then the acting node (--node), then the email, then "unknown". Self-reported — a
signature or token would make it verifiable; this only stops the acting node from
being lost. Pure function, no DB, judged by exit code.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tabus import cli as tabc  # noqa: E402

fails = []


def check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        fails.append(name)


A = tabc._audit_actor

check("explicit --by wins over node and email", A("dan", "cool-node", "e@x.com") == "dan")
check("acting node is recorded when no --by", A(None, "dan", "owner@example.com") == "dan")
# 🔴 the exact laundering this guards against: without it, the owner email would be stored
check("🔴 node beats owner email (no laundering)", A(None, "dan", "owner@example.com") != "owner@example.com")
check("email is the fallback only when no --by and no node", A(None, None, "e@x.com") == "e@x.com")
check("unknown when nothing is known", A(None, None, None) == "unknown")
# empty strings are not an actor — they must fall through, not be recorded as ""
check("empty strings fall through to unknown", A("", "", "") == "unknown")
check("empty --by falls through to node", A("", "dan", "e@x.com") == "dan")

print(f"\n{'FAIL' if fails else 'ALL PASS'} — {len(fails)} failed")
sys.exit(1 if fails else 0)
