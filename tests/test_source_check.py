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
# A made-up name, added for these runs only: no real name is written in this file.
NAME = "zorblax"
OTHER = "quillon"
ENV = dict(os.environ, TABC_SOURCE_CHECK_NAMES=f"{NAME},{OTHER}")


def run(*paths):
    return subprocess.run([sys.executable, TOOL, *paths], capture_output=True, text=True, cwd=ROOT, env=ENV)


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
            f"    def test_x(self):  # {NAME} 4c84df2c\n",
            f"# {NAME} found the case; the rule is now one place.\n",
            f"# 0 bytes back ({NAME} review J1).\n",
            f'"""The reproduction {OTHER} and {NAME} sent, run against tabd."""\n',
            f"# S3 must stay CLAIMED ({NAME.capitalize()}'s finding).\n",
            f"# verify all {NAME.capitalize()}'s criteria; exit 1 on failure.\n",
            f"# defined once in the file ({NAME.capitalize()}).\n",
            f"# fall back to pull, per {NAME.capitalize()}'s 475/476 findings.\n",
            f"{NAME.capitalize()}, 2026-08-26: only the last value was kept.\n",
        ):
            r = self.check_text(line)
            self.assertEqual(r.returncode, 1, line + r.stdout)
            self.assertIn("FOUND   internal name", r.stdout, line)

    def test_leaves_ordinary_text(self):
        for line in (
            "# a hex id alone (9d72d474) is not a credit.\n",
            "# see the table (exit codes) above.\n",
            "x = f(a, b)  # (warm up period)\n",
            f"# the {NAME} module is loaded first, then the rest\n",  # a name in a sentence is not a credit
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

    def load(self):
        # The module is a script; take its definitions without running the report.
        ns = {}
        with open(TOOL, encoding="utf-8") as f:
            exec(compile(f.read().split("\nfound = {}")[0], TOOL, "exec"), ns)
        return ns

    def test_names_are_held_as_digests(self):
        ns = self.load()
        digests = ns["NAME_DIGESTS"]
        self.assertTrue(digests)
        self.assertTrue(all(len(d) == ns["NAME_DIGEST_HEX"] and int(d, 16) >= 0 for d in digests))
        # The checker itself writes no name out: none of its words is a name.
        with open(TOOL, encoding="utf-8") as f:
            words = set(ns["WORD"].findall(f.read()))
        self.assertEqual(sorted(w for w in words if ns["is_name"](w) and w.lower() not in (NAME, OTHER)), [])

    def git_repo_with(self, d, name, text):
        subprocess.run(["git", "init", "-q", d], check=True)
        with open(os.path.join(d, name), "w", encoding="utf-8") as f:
            f.write(text)
        subprocess.run(["git", "-C", d, "add", name], check=True)

    def test_tracked_file_without_suffix_is_checked(self):
        with tempfile.TemporaryDirectory() as d:
            self.git_repo_with(d, "LICENSE", f"MIT. Reviewed by {NAME} 1234abcd.\n")
            r = subprocess.run([sys.executable, TOOL], capture_output=True, text=True, cwd=d, env=ENV)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("LICENSE", r.stdout)

    def test_directory_argument_includes_files_without_suffix(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "LICENSE"), "w", encoding="utf-8") as f:
                f.write(f"# {NAME} 1234abcd\n")
            r = run(d)
        self.assertEqual(r.returncode, 1, r.stdout)

    def test_git_failure_is_not_zero_findings(self):
        with tempfile.TemporaryDirectory() as d:
            self.git_repo_with(d, "LICENSE", f"# {NAME} 1234abcd\n")
            fake = os.path.join(d, "bin")
            os.makedirs(fake)
            with open(os.path.join(fake, "git"), "w") as f:
                f.write("#!/bin/sh\necho fatal: simulated >&2\nexit 128\n")
            os.chmod(os.path.join(fake, "git"), 0o755)
            bad = subprocess.run([sys.executable, TOOL], capture_output=True, text=True, cwd=d,
                                 env=dict(ENV, PATH=fake + os.pathsep + os.environ["PATH"]))
            none = subprocess.run([sys.executable, TOOL], capture_output=True, text=True, cwd=d,
                                  env=dict(ENV, PATH="/nonexistent"))
        for r in (bad, none):
            self.assertEqual(r.returncode, 2, r.stdout)
            self.assertIn("not checked", r.stdout)
            self.assertNotIn("0 findings", r.stdout)

    def test_published_code_has_no_credits(self):
        # Every file git tracks (no paths = git ls-files), tests and docs included.
        r = run()
        self.assertEqual(r.returncode, 0, r.stdout)


if __name__ == "__main__":
    unittest.main()
