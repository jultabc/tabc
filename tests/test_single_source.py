"""Defaults that several programs share are written once.

The bus port, the thread page limits and the doorbell interval each had copies in
two to four files. A change to one copy would leave the others behind, and the
programs would disagree without an error. This test fails when a copy comes back.
"""

import inspect
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tabus import cli, daemon, doorbell, doorbell_poll, paths, reply_link  # noqa: E402

CODE_DIRS = ("tabus", "scripts", "clients")


def code_lines():
    for top in CODE_DIRS:
        for d, dirs, files in os.walk(os.path.join(ROOT, top)):
            dirs[:] = [x for x in dirs if x not in ("__pycache__", "build", ".gradle")]
            for fn in files:
                if fn.endswith((".py", ".java", ".kt", ".sh")):
                    path = os.path.join(d, fn)
                    with open(path, encoding="utf-8", errors="replace") as f:
                        for n, line in enumerate(f, 1):
                            yield os.path.relpath(path, ROOT), n, line


def reads_env(name, line):
    """A line that reads the variable (environ.get / environ[...] / getenv), not one that sets it for a child."""
    return re.search(r"""(environ\.get|getenv|environ\[)\(?\s*["']%s["']""" % name, line) is not None and "put(" not in line


class SingleSource(unittest.TestCase):
    def test_bus_port_is_written_once(self):
        hits = [f"{p}:{n}" for p, n, line in code_lines() if re.search(r"\b%d\b" % paths.DEFAULT_BUS_PORT, line)]
        self.assertEqual(hits, ["tabus/paths.py:%d" % self._line_of("tabus/paths.py", "DEFAULT_BUS_PORT =")], hits)

    def test_loopback_host_is_written_once(self):
        # The default host tabd binds and clients dial, in the Python package and
        # scripts. mcp_server's allow-list of loopback names is a different rule and
        # is not counted. The demo shell script and the Java tests start their own
        # servers on their own ports and are not counted either.
        allowed = ("tabus/mcp_server.py", 'base.hostname not in ("localhost", "127.0.0.1", "::1")')
        hits = [f"{p}:{n}" for p, n, line in code_lines()
                if p.endswith(".py") and re.search(r"(?<![\d.])127\.0\.0\.1(?![\d.])", line)
                and not (p == allowed[0] and allowed[1] in line)]
        self.assertEqual(hits, ["tabus/paths.py:%d" % self._line_of("tabus/paths.py", "DEFAULT_BUS_HOST =")], hits)

    def test_doorbell_interval_default_is_written_once(self):
        hits = [f"{p}:{n}" for p, n, line in code_lines() if reads_env("DOORBELL_POLL_SEC", line)]
        self.assertEqual(len(hits), 1, hits)
        self.assertTrue(hits[0].startswith("tabus/paths.py:"), hits)

    def test_settings_are_read_only_through_paths(self):
        for name in ("TABC_BUS_URL",):
            hits = [f"{p}:{n}" for p, n, line in code_lines() if reads_env(name, line)]
            self.assertTrue(hits and all(h.startswith("tabus/paths.py:") for h in hits), hits)

    def test_readers_use_the_shared_values(self):
        self.assertTrue(paths.DEFAULT_BUS_URL.endswith(":%d" % paths.DEFAULT_BUS_PORT))
        self.assertEqual(cli.THREAD_CLI_LIMIT, reply_link.THREAD_PAGE_DEFAULT)
        self.assertIn("doorbell_poll.poll_interval()", inspect.getsource(doorbell.main))
        self.assertIn("_config_port()", inspect.getsource(daemon.main))


    def test_thread_limit_text_matches_the_limit(self):
        # The CLI help is built from reply_link; the MCP tool description is a docstring
        # (shown to clients as written), so it is pinned here instead.
        self.assertIn("1 to %d" % reply_link.THREAD_PAGE_MAX, self._mcp_thread_doc())
        with open(os.path.join(ROOT, "tabus", "mcp_server.py"), encoding="utf-8") as f:
            self.assertIn("_limit(limit, THREAD_PAGE_MAX)", f.read())

    def _mcp_thread_doc(self):
        with open(os.path.join(ROOT, "tabus", "mcp_server.py"), encoding="utf-8") as f:
            src = f.read()
        m = re.search(r'def tabc_thread\(.*?"""(.*?)"""', src, re.S)
        self.assertIsNotNone(m)
        return m.group(1)

    def _line_of(self, rel, needle):
        with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
            for n, line in enumerate(f, 1):
                if needle in line:
                    return n
        self.fail(f"{needle} not in {rel}")


if __name__ == "__main__":
    unittest.main()
