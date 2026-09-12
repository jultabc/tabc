#!/usr/bin/env python3
"""The release version is written twice; this checks the two agree.

`pyproject.toml` decides what pip installs and what PyPI lists. `tabus.bus`
decides what `tabc --version` and `tabd --version` print. Nothing tied them
together, and they drifted: 0.1.0.post1 shipped while the command reported
0.1.0, so the number a user could read was not the number they had.

A comment saying "keep these in sync" goes stale the moment someone bumps one
of them. This fails the build instead.

Hermetic: reads two files, no database, no network. Check the exit code."""

import os
import re
import sys

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, root)
import tabus  # noqa: E402

fails = []


def check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        fails.append(name)


src = open(os.path.join(root, "pyproject.toml"), encoding="utf-8").read()
# Only the [project] table's own version. A regex is enough here and keeps the
# test runnable on 3.9, which has no tomllib.
project = src.split("[project]", 1)[1].split("\n[", 1)[0]
m = re.search(r'^version\s*=\s*"([^"]+)"', project, re.M)

check("pyproject declares a version", m is not None)
if m:
    declared = m.group(1)
    reported = tabus.__version__
    check(
        f"pyproject {declared} == tabus.__version__ {reported}",
        declared == reported,
    )
    # The fallback in tabus/__init__.py hides a missing attribute behind "0".
    # That would make the comparison above pass for the wrong reason only if
    # pyproject also said "0", so name it rather than trust it.
    check("version is not the missing-attribute fallback", reported != "0")

print("FAIL" if fails else "PASS")
sys.exit(1 if fails else 0)
