"""Foreground CLI evidence rejects server children and unrecognized binaries."""
import sys
import unittest
import ctypes
from unittest.mock import Mock, patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tabus import terminal_frontend as frontend


class FrontendTests(unittest.TestCase):
    def probe(self, *, info=None, path=None):
        return frontend.foreground_frontend_tty(
            20, "ttys000", inspect=lambda _: info or (501, 10, 20, 20, "ttys000", ["codex"]),
            executable=lambda _: path or "/users/test/.codex/packages/standalone/releases/0.153.4/bin/codex",
            uid=501, user_home="/users/test")

    def test_foreground_native_cli(self):
        self.assertEqual(self.probe(), "ttys000")
        self.assertEqual(self.probe(info=(501, 10, 20, 20, "ttys000", ["claude"]),
                                   path="/opt/homebrew/Caskroom/claude-code@latest/2.1.259/claude"), "ttys000")

    def test_shared_server_first_child_is_rejected_on_empty_route(self):
        for args in (["codex", "app-server"], ["codex", "mcp-server"], ["codex", "exec"],
                     ["claude", "-p"], ["claude", "--print"], ["claude", "--sdk-url", "http://host"]):
            with self.subTest(args=args):
                path = ("/opt/homebrew/Caskroom/claude-code@latest/2.1.259/claude"
                        if args[0] == "claude" else None)
                self.assertIsNone(self.probe(info=(501, 10, 20, 20, "ttys000", args), path=path))

    def test_detached_command_accepts_only_checked_immediate_frontend(self):
        from tabus import route_capture
        rows = {30: (20, "??"), 20: (10, "ttys000")}
        checked = []
        tty = route_capture.terminal_tty(start_pid=30, process_row=rows.get, streams=(),
            session_id=30, process_session=lambda _: 10,
            frontend_tty=lambda pid, tty: checked.append((pid, tty)) or self.probe())
        self.assertEqual(tty, "ttys000")
        self.assertEqual(checked, [(20, "ttys000")])
        rows[20] = (10, "??")
        rows[10] = (1, "ttys000")
        checked.clear()
        tty = route_capture.terminal_tty(start_pid=30, process_row=rows.get, streams=(),
            session_id=30, process_session=lambda _: 10,
            frontend_tty=lambda pid, tty: checked.append((pid, tty)) or "ttys000")
        self.assertIsNone(tty)
        self.assertEqual(checked, [])

    def test_mcp_child_background_wrong_tty_and_other_uid(self):
        for row in ((501, 10, 10, 10, "ttys000", ["codex"]),
                    (501, 10, 20, 30, "ttys000", ["codex"]),
                    (501, 10, 20, 20, "ttys999", ["codex"]),
                    (502, 10, 20, 20, "ttys000", ["codex"])):
            self.assertIsNone(self.probe(info=row))

    def test_name_alone_is_insufficient(self):
        for path in ("/tmp/codex", "/tmp/claude", "/usr/bin/python3",
                     "/users/test/.codex/packages/standalone/releases/0.1/bin/codex-extra"):
            self.assertIsNone(self.probe(path=path))

    def test_resume_modes_and_unknown_launches(self):
        identifier = "9c128700-6f6d-4d47-abcc-2a9de98a9336"
        for kind, args in (("codex", ["codex", "resume", identifier]),
                           ("codex", ["codex", "resume", "coco"]),
                           ("claude", ["claude", "--resume", identifier])):
            self.assertTrue(frontend.interactive_arguments(kind, args))
        for args in (["codex", "app-server"], ["codex", "unknown"], ["codex", "-m"],
                     ["codex", "resume", "app-server"]):
            self.assertFalse(frontend.interactive_arguments("codex", args))

    def test_equal_and_separate_value_options_match(self):
        for kind, option, value in (("codex", "--model", "gpt-5"),
                                    ("codex", "--cd", "/workspace"),
                                    ("claude", "--model", "opus"),
                                    ("claude", "--permission-mode", "plan"),
                                    ("claude", "--resume", "agent-session")):
            for args in ([kind, option, value], [kind, option + "=" + value]):
                with self.subTest(args=args):
                    self.assertTrue(frontend.interactive_arguments(kind, args))
                    self.assertFalse(frontend.interactive_arguments(kind, args + ["app-server"]))
        for args in (["claude", "--print=true"], ["claude", "--model="],
                     ["claude", "--model", "--print"], ["claude", "--sdk-url=http://host"],
                     ["claude", "--unknown=value"], ["claude", "--resume=app-server"]):
            self.assertFalse(frontend.interactive_arguments("claude", args))

    def test_process_change_fails_closed(self):
        rows = iter([(501, 10, 20, 20, "ttys000", ["codex"]), None])
        self.assertIsNone(frontend.foreground_frontend_tty(
            20, "ttys000", inspect=lambda _: next(rows),
            executable=lambda _: "/users/test/.codex/packages/standalone/releases/0.153.4/bin/codex",
            uid=501, user_home="/users/test"))

    def test_frontend_change_during_terminal_query_is_rejected(self):
        from tabus import route_capture
        replies = iter(["ttys000", None])
        decision = route_capture.inspect_route(
            {"ITERM_SESSION_ID": "pane"}, terminal_tty=lambda: next(replies),
            route_tty=lambda _adapter, _target: "ttys000")
        self.assertTrue(decision.rejected)

    def test_ctypes_argument_failure_is_caught(self):
        def fail(_pid):
            raise ctypes.ArgumentError("invalid C argument")
        self.assertIsNone(frontend.foreground_frontend_tty(
            20, "ttys000", inspect=fail, uid=501, user_home="/users/test"))

    def test_proc_pidpath_declares_signature(self):
        function = Mock(return_value=0)
        library = Mock(proc_pidpath=function)
        with patch("tabus.terminal_frontend.sys.platform", "darwin"), \
             patch("tabus.terminal_frontend.ctypes.CDLL", return_value=library):
            self.assertIsNone(frontend.native_executable(123))
        self.assertEqual(function.argtypes, [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32])
        self.assertIs(function.restype, ctypes.c_int)


if __name__ == "__main__":
    unittest.main()
