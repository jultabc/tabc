"""Argument boundaries, including spaces and quotes, survive OS inspection."""
import ctypes
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tabus.process_argv import native_arguments, parse_darwin_arguments
from tabus.terminal_frontend import interactive_arguments


class ArgumentTests(unittest.TestCase):
    def packed(self, args, suffix=b"PRIVATE_ENV=not-an-argument\0"):
        return (len(args).to_bytes(ctypes.sizeof(ctypes.c_int), sys.byteorder, signed=True)
                + b"/native/codex\0\0\0" + b"\0".join(os.fsencode(arg) for arg in args)
                + b"\0" + suffix)

    def test_spaces_quotes_and_backslashes_are_not_shell_syntax(self):
        for path in ("/tmp/user/My Drive/work", "/tmp/one'quote", '/tmp/two"quotes',
                     "/tmp/back\\slash", "/tmp/app-server workspace"):
            args = ["codex", "--cd", path]
            actual = parse_darwin_arguments(self.packed(args))
            self.assertEqual(actual, args)
            self.assertTrue(interactive_arguments("codex", actual))
            self.assertFalse(interactive_arguments("codex", actual + ["exec"]))

    def test_environment_is_not_returned(self):
        self.assertEqual(parse_darwin_arguments(self.packed(["codex", "resume", "coco"])),
                         ["codex", "resume", "coco"])

    def test_empty_value_is_preserved_then_rejected(self):
        actual = parse_darwin_arguments(self.packed(["codex", "--model", ""]))
        self.assertEqual(actual, ["codex", "--model", ""])
        self.assertFalse(interactive_arguments("codex", actual))

    def test_truncation_and_invalid_count_fail_closed(self):
        for raw in (b"", b"\0" * 4, self.packed(["codex"], suffix=b"")[:-1],
                    (-1).to_bytes(4, sys.byteorder, signed=True) + b"/exec\0"):
            self.assertIsNone(parse_darwin_arguments(raw))

    def test_sysctl_declares_size_t_and_pointer_signature(self):
        function = Mock(return_value=-1)
        library = Mock(sysctl=function)
        with patch("tabus.process_argv.sys.platform", "darwin"), \
             patch("tabus.process_argv.ctypes.CDLL", return_value=library):
            self.assertIsNone(native_arguments(123))
        self.assertEqual(function.argtypes, [ctypes.POINTER(ctypes.c_int), ctypes.c_uint,
                         ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
                         ctypes.c_void_p, ctypes.c_size_t])
        self.assertIs(function.restype, ctypes.c_int)

    @unittest.skipUnless(sys.platform == "darwin" or sys.platform.startswith("linux"), "OS argv API")
    def test_real_child_preserves_boundaries(self):
        args = ["/tmp/My Drive/work", "quote'and\"double", "back\\slash", ""]
        proc = subprocess.Popen([sys.executable, "-c", "import sys;sys.stdin.read()", *args],
                                stdin=subprocess.PIPE)
        try:
            actual = native_arguments(proc.pid)
            self.assertIsNotNone(actual)
            self.assertEqual(actual[-len(args):], args)
        finally:
            proc.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
