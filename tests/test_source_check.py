#!/usr/bin/env python3
"""scripts/source_check.py finds internal names in credits and does not cry wolf.

Shapes it must find: a name with a review id, a parenthesis of names only, a name
with a date. Shapes it must leave: ordinary prose in parentheses, a hex id with no
name. And the package itself must have none, so a credit cannot come back.
"""

import os
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "scripts", "source_check.py")
NAME = "ja" + "ck"  # spelled apart so this file is not a finding itself


def run(*paths):
    return subprocess.run([sys.executable, TOOL, *paths], capture_output=True, text=True, cwd=ROOT)


class SourceCheck(unittest.TestCase):
    def check_text(self, text):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.py")
            with open(p, "w", encoding="utf-8") as f:
                f.write(text)
            return run(d)

    def test_finds_credit_shapes(self):
        for line in (
            f"# refuses before the gate ({NAME} 9d72d474 N2).\n",
            f"# the same sentence would drift ({NAME}).\n",
            f"# a request was stored ({NAME}, {NAME} M3).\n",
            f"# measured ({NAME} 2026-11-23).\n",
        ):
            r = self.check_text(line)
            self.assertEqual(r.returncode, 1, line + r.stdout)
            self.assertIn("FOUND   internal name", r.stdout, line)

    def test_leaves_ordinary_text(self):
        for line in (
            "# a hex id alone (9d72d474) is not a credit.\n",
            "# see the table (exit codes) above.\n",
            "x = f(a, b)  # (cole down period)\n",
        ):
            r = self.check_text(line)
            self.assertNotIn("FOUND   internal name", r.stdout, line)

    def test_unreadable_file_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "bad.py"), "wb") as f:
                f.write(b"\xff\xfe not utf-8")
            r = run(d)
        self.assertIn("FOUND   a file that could not be read", r.stdout)
        self.assertEqual(r.returncode, 1)

    def test_report_limits_are_named_once(self):
        # Excerpt length, rows shown and context widths live in named constants;
        # a literal slice or offset would be a second copy that drifts.
        with open(TOOL, encoding="utf-8") as f:
            code = [l for l in f if not l.lstrip().startswith("#")]
        text = "".join(code)
        self.assertEqual(re.findall(r"\[:\d+\]", text), [])
        self.assertEqual(re.findall(r"m\.(?:start|end)\(\)\s*[-+]\s*\d", text), [])
        self.assertEqual(re.findall(r"len\(rows\)\s*[-+>]=?\s*\d", text), [])

    def test_published_code_has_no_credits(self):
        # Everything in the public repository that holds code, tests included.
        r = run("tabus", "scripts", "clients", "tests")
        self.assertEqual(r.returncode, 0, r.stdout)


if __name__ == "__main__":
    unittest.main()
