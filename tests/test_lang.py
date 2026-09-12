#!/usr/bin/env python3
"""tabuslang localization: t() Korean, English, fallback, invalid-input safety, and English alarm integration.

t() serves live notifier calls and must handle missing keys, format mismatches, and unknown languages without crashing.
Optional Korean output is checked explicitly; missing release translations fall back to English.
Run directly and check the exit code."""

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BEACON = os.path.dirname(HERE)  # 
sys.path.insert(0, BEACON)
os.environ["DOORBELL_LEDGER"] = os.path.join(tempfile.mkdtemp(), "ring.db")

from tabus import lang as tabuslang  # noqa: E402

fails = []


def check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        fails.append(name)


print("t() defaults, languages, and fallback")
os.environ.pop("TABC_LANG", None)
check("1 English is the default language", tabuslang.current_lang() == "en" and tabuslang.t("who.unverified") == "[unverified]")
check(
    "2 en(lang=) — [unverified]",
    tabuslang.t("who.unverified", lang="en") == "[unverified]",
)

os.environ["TABC_LANG"] = "en"
check(
    "3 respects env=en",
    tabuslang.current_lang() == "en"
    and tabuslang.t("who.unverified") == "[unverified]",
)
os.environ.pop("TABC_LANG", None)

# Removed old test 4 for who.and_more because that unused key was removed; only the representative sender is shown.
# The branch appending a count of additional senders was removed entirely.

# Korean alarm regression: preserve existing output, with the sender label and name at the end.
check(
    "5 Korean alarm output is unchanged",
    tabuslang.t("doorbell.ring", who='erin[\ubbf8\ud655\uc778]', unread=3, lang="ko")
    == (
        '[dm] \ubbf8\uc77d\uc74c 3\uac74 \xb7 \ubcf4\ub0b8\uc774: erin[\ubbf8\ud655\uc778]'
        if tabuslang._catalog("ko")
        else '[dm] 3 unread · from: erin[\ubbf8\ud655\uc778]'
    ),
)
check(
    "5 en ring",
    tabuslang.t("doorbell.ring", who="erin[unverified]", unread=3, lang="en")
    == "[dm] 3 unread · from: erin[unverified]",
)

print("Fallback must not crash")
check("6 missing key returns its name, not silence", tabuslang.t("no.such.key") == "no.such.key")
check(
    "7 unknown language zz falls back to English", tabuslang.t("who.unverified", lang="zz") == "[unverified]"
)
check(
    "8 missing format arguments return the original text without crashing",
    tabuslang.t("doorbell.ring")
    == '[dm] {unread} unread · from: {who}',
)

# 9. English and Korean key sets match to prevent missing translations and excessive fallback.
ko_keys = set(tabuslang._catalog("ko").keys())
en_keys = set(tabuslang._catalog("en").keys())
check("9 English exists and any optional Korean catalogue has matching keys", bool(en_keys) and (not ko_keys or en_keys == ko_keys))

print("English alarm integration")
from tabus import doorbell as va  # noqa: E402

# Show only senders[0], even with two senders in the input.
# An alarm is a new knock, not an inbox summary; no additional-sender count suffix.
senders = [
    {"sender": "bob", "count": 3, "verified": False},
    {"sender": "alice", "count": 1, "verified": False},
]
os.environ.pop("TABC_LANG", None)
check(
    "10 ko who = 'bob' (representative only; verified signature means no unverified marker)",
    va.format_doorbell_who(senders) == "bob",
)

os.environ["TABC_LANG"] = "en"
check(
    "11 en who = 'bob' (representative only; no marker)",
    va.format_doorbell_who(senders) == "bob",
)

captured = {}


def _cap(target, msg, enter=True):
    captured["msg"] = msg
    return (va.DELIVER_SUCCESS, 1)


va.unread_senders = lambda n: [
    {"sender": "bob", "count": 2, "verified": False},
    {"sender": "alice", "count": 5, "verified": False},
]
va.route_for = lambda n: ("iterm2", "w0:GUID")
va.send_to_iterm_session = _cap
va.deliver_doorbell("erin", 7)
check(
    "12 en multiple senders still show bob's unread count of 2",
    captured.get("msg")
    == "[dm] 2 unread · from: bob",
)

os.environ["TABC_LANG"] = "ko"
va.deliver_doorbell("erin", 7)
check(
    "13 ko multiple senders still show bob's unread count of 2",
    captured.get("msg") == (
        '[dm] \ubbf8\uc77d\uc74c 2\uac74 \xb7 \ubcf4\ub0b8\uc774: bob'
        if tabuslang._catalog("ko")
        else '[dm] 2 unread · from: bob'
    ),
)

os.environ["TABC_LANG"] = "en"
va.unread_senders = lambda n: []
captured.clear()
result = va.deliver_doorbell("erin", 7)
check(
    "14 empty snapshot does not inject stale metadata",
    result[0] == "STALE" and not captured,
)

va.unread_senders = lambda n: [{}]
captured.clear()
malformed_safe = False
try:
    result = va.deliver_doorbell("erin", 7)
    malformed_safe = result[0] == "UNKNOWN" and not captured
except (KeyError, IndexError, TypeError):
    pass
check("15 malformed metadata is retried without injection", malformed_safe)

va.unread_senders = lambda n: None
result = va.deliver_doorbell("erin", 7)
check(
    "16 lookup failure is retried without injection",
    result[0] == "UNKNOWN" and not captured,
)
line_breaks = ("\n", "\r", "\x0b", "\x0c", "\u2028", "\u2029")
check(
    "17 all alarm translations contain no line separators that could act as Enter",
    all(
        not any(char in value for char in line_breaks)
        for lang in ("ko", "en")
        for value in tabuslang._catalog(lang).values()
    ),
)
os.environ.pop("TABC_LANG", None)

print(f"\n{'❌ FAIL' if fails else '✅ ALL PASS'} — {len(fails)} failed")
sys.exit(1 if fails else 0)
