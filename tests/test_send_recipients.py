#!/usr/bin/env python3
"""fn_send recipient parsing regression tests: prevent silent loss of repeated flags.
Cool, 2026-08-26: argparse retained only the last --to value, so 75% of that day's sends reached only one recipient.
Regression tests guard this silent failure. Run directly and check the exit code; no network."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.argv = ["tabc"]
from tabus import cli as tabc  # noqa: E402

ap = tabc.build_parser()


def parse_to(argv):
    return ap.parse_args(
        ["send", "--sender", "x"] + argv + ["--subject", "s", "--body", "b"]
    ).to


def parsed(command):
    return ap.parse_args(
        [
            command,
            "--sender",
            "x",
            "--to",
            "a,b",
            "--subject",
            "s",
            "--body",
            "b",
            "--priority",
            "now",
        ]
    )


P = 0
F = 0


def ck(name, got, want):
    global P, F
    if got == want:
        P += 1
        print(f"  ✓ {name}")
    else:
        F += 1
        print(f"  ✗ FAIL {name}: got {got} want {want}")


ck('dm\uc740 \ubc1b\uc740 \ubaa9\ub85d', tabc.COMMANDS["dm"][0] is tabc.fn_mailbox, True)
ck('mailbox \uc774\ub984 \uc81c\uac70', "mailbox" in tabc.COMMANDS, False)
ck('dm \uc870\ud68c \uc778\uc790', tabc.build_parser().parse_args(["dm", "--node", "a"]).node, "a")


# Core regression: repeated flags must not retain only the final recipient.
ck(
    '\ubc18\ubcf5\ud50c\ub798\uadf8 \ub204\uc801',
    tabc._recipients(parse_to(["--to", "a", "--to", "b"])),
    ["a", "b"],
)
ck('\ucf64\ub9c8 \ubd84\ud574', tabc._recipients(parse_to(["--to", "a,b"])), ["a", "b"])
ck(
    '\ud63c\ud569(\ubc18\ubcf5+\ucf64\ub9c8)',
    tabc._recipients(parse_to(["--to", "a,b", "--to", "c"])),
    ["a", "b", "c"],
)
ck(
    '\uc21c\uc11c\ubcf4\uc874 \uc911\ubcf5\uc81c\uac70',
    tabc._recipients(parse_to(["--to", "a", "--to", "a,b"])),
    ["a", "b"],
)
ck('\ube48\uce78/\uacf5\ubc31 \uc81c\uac70', tabc._recipients(["a, ,b"]), ["a", "b"])
ck('\ube48 \uc785\ub825', tabc._recipients([]), [])
ck('None \uc548\uc804', tabc._recipients(None), [])

# Require a target: fn_send raises SystemExit without --to, --tac, or --broadcast.
# argparse treats --to as optional because --tac and --broadcast are alternatives; fn_send enforces the target requirement.
# Run the actual handler because parse_args alone cannot catch this.
_args = ap.parse_args(["send", "--sender", "x", "--subject", "s", "--body", "b"])
try:
    tabc.fn_send(_args)
    ck('\uc218\uc2e0\uc790 \ud544\uc218', "no-error", "SystemExit")
except SystemExit:
    ck('\uc218\uc2e0\uc790 \ud544\uc218(\ud0c0\uac9f \uc5c6\uc73c\uba74 \uac70\ubd80)', "SystemExit", "SystemExit")

# Anchor _delivery_report branches so the delivery echo cannot silently disappear.
dr = tabc._delivery_report
ck(
    '\ubc30\ub2ec\ubcf4\uace0: \uc804\uc6d0 \uc800\uc7a5',
    dr({"recipients": ["a", "b"]}, ["a", "b"]),
    ["stored for: a, b (2)"],
)
_r2 = dr({"recipients": ["a"]}, ["a", "ghost"])
ck(
    '\ubc30\ub2ec\ubcf4\uace0: \ubbf8\ub4f1\ub85d \uacbd\uace0 2\uc904',
    len(_r2) == 2 and "ghost" in _r2[1] and "not delivered" in _r2[1],
    True,
)
ck(
    '\ubc30\ub2ec\ubcf4\uace0: \uba71\ub4f1 str \ud1b5\uacfc',
    dr("identical envelope resent — not stored twice", ["a"]),
    ["(identical envelope resent — not stored twice)"],
)
# Missing or malformed info must produce an unverifiable marker, not silent success.
ck('\ubc30\ub2ec\ubcf4\uace0: info None \u2192 \ud655\uc778\ubd88\uac00 \ud45c\uc2dd', "cannot confirm" in dr(None, ["a"])[0], True)
ck(
    '\ubc30\ub2ec\ubcf4\uace0: recipients \uc5c6\ub294 dict \u2192 \ud45c\uc2dd',
    "cannot confirm" in dr({"id": "x"}, ["a"])[0],
    True,
)

print(f"\n{'=' * 40}\nPASS {P} / FAIL {F}")
sys.exit(0 if F == 0 else 1)
