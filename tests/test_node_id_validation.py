#!/usr/bin/env python3
"""Node id validation — the server rejects unsafe names, one rule shared with the key path.

cool found it: the client's signed path rejected an unsafe name (nodekey.key_path),
but a direct POST /register accepted whitespace, newlines, and ../../etc/x — and a
newline rode the name into the doorbell's auto-typed line. This pins the fix: one
predicate (nodekey.is_safe_node_id) used by both the key path and bus_register, so
the server and the key path can never disagree; and the doorbell's typing strips
control characters as a second layer. Temp DB, pure, judged by exit code.
"""

import os
import sys
import tempfile

os.environ["TABC_DB"] = os.path.join(tempfile.mkdtemp(prefix="nodeid_"), "t.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tabus  # noqa: E402
from tabus import nodekey as nodekey  # noqa: E402
from tabus import doorbell as doorbelld  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{(' — ' + detail) if detail else ''}")
    if not cond:
        fails.append(name)


# ── 1. the shared predicate ──
ok_names = ["dan", "cool", "jack-pgm-test", "node_1", "A1"]
bad_names = ["a b", "a\nb", "a\tb", "../x", "a/b", "a.b", "", "  ", "x\r", None, 123]
check("safe names pass", all(nodekey.is_safe_node_id(n) for n in ok_names))
check("🔴 unsafe names rejected (space/newline/tab/traversal/dot/empty/non-str)",
      all(not nodekey.is_safe_node_id(n) for n in bad_names))
# the newline one specifically — the doorbell-injection vector
check("🔴 a newline name is rejected", not nodekey.is_safe_node_id("evil\nrm -rf"))
# 🔴 homograph: Cyrillic 'а' (U+0430) looks like Latin 'a' but is a different string.
#    isalnum() alone is Unicode and would pass it; the ascii guard rejects it so a
#    name cannot impersonate another on a doorbell line by look-alike.
check("Latin 'alice' passes", nodekey.is_safe_node_id("alice"))
check("🔴 Cyrillic look-alike 'аlice' is rejected", not nodekey.is_safe_node_id("аlice"))

# ── 2. key_path uses the same rule (rejects unsafe, accepts safe) ──
try:
    nodekey.key_path("a\nb")
    check("key_path rejects a newline name", False)
except ValueError:
    check("key_path rejects a newline name", True)
check("key_path accepts a safe name", nodekey.key_path("dan").endswith(".node_key.dan"))

# ── 3. bus_register enforces it server-side (the gap cool found) ──
con = tabus.connect()
con.executescript(tabus.SCHEMA)
tabus.migrate(con)
ok, msg = tabus.bus_register(con, "dan", "generic")
check("register accepts a safe name", ok, msg)
for bad in ["a b", "evil\nrm -rf", "../../etc/x", ""]:
    okb, mb = tabus.bus_register(con, bad, "generic")
    check(f"🔴 register refuses {bad!r}", not okb, mb)
# the refused names left no node row
cnt = con.execute("SELECT count(*) FROM nodes WHERE node_id != 'dan'").fetchone()[0]
check("🔴 no unsafe node row was created", cnt == 0, f"extra rows={cnt}")
con.close()

# ── 4. doorbell typing strips control chars (second layer) ──
check("🔴 _single_line drops a newline", "\n" not in doorbelld._single_line("a\nb"))
check("_single_line drops CR and tab", doorbelld._single_line("a\r\tb") == "ab")
check("_single_line keeps printable text", doorbelld._single_line('cool[\ubbf8\ud655\uc778] (3)') == 'cool[\ubbf8\ud655\uc778] (3)')

print(f"\n{'FAIL' if fails else 'ALL PASS'} — {len(fails)} failed")
sys.exit(1 if fails else 0)
