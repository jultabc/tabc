"""Isolated queue transport tests. Never invoke a live Codex instance."""

import os
import contextlib
import io
import subprocess
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tabus.codex_queue import QueueDelivery, current_thread, thread_id, capture_current_thread

THREAD = "01a05ed9-0b43-7e41-a3d9-0cdd8361abff"
OTHER = "01a05ed9-0b43-7e41-a3d9-0cdd8361abfe"
SUBMISSION = "01a13a44-2229-7ed0-9539-29b1c95faddb"


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = os.path.join(self.temp.name, "receipts.db")
        self.run = Mock(return_value=SimpleNamespace(returncode=0, stdout=
            f"Queued message {SUBMISSION} for thread {THREAD}.\n"))
        self.queue = self.open()

    def open(self):
        queue = QueueDelivery(self.path, "/trusted/codex", self.run)
        self.addCleanup(queue.close)
        return queue

    def send(self, **kwargs):
        return self.queue.deliver(THREAD, "hu:mail-42", "[tabc] unread", **kwargs)

    def test_uuid_only(self):
        for bad in (None, "hu", "--last", THREAD.upper(), THREAD + "\n"):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                thread_id(bad)

    def test_current_thread_does_not_use_iterm_or_root_session(self):
        self.assertEqual(current_thread({"CODEX_THREAD_ID": THREAD,
            "CODEX_SESSION_ID": OTHER, "ITERM_SESSION_ID": "stale"}), THREAD)
        with self.assertRaises(ValueError):
            current_thread({"CODEX_SESSION_ID": THREAD})

    def test_submission_requires_literal_opt_in(self):
        for opt in (False, None, "on", 1):
            self.assertEqual(self.send(auto_enter=opt), ("DISABLED", None))
        self.run.assert_not_called()

    def test_acceptance_is_queued_not_read(self):
        self.assertEqual(self.send(auto_enter=True), ("QUEUED", SUBMISSION))
        args, kwargs = self.run.call_args
        self.assertEqual(args[0], ["/trusted/codex", "queue", "--remote=unix://",
                                  f"--thread={THREAD}", "--message=[tabc] unread"])
        self.assertNotIn("shell", kwargs)

    def test_leading_dash_message_is_one_option_value(self):
        for index, message in enumerate(("-x probe", "--help", "--thread=other", "-")):
            self.assertEqual(self.queue.deliver(THREAD, f"dash-{index}", message, auto_enter=True),
                             ("QUEUED", SUBMISSION))
            self.assertEqual(self.run.call_args.args[0][-1], f"--message={message}")
            self.assertEqual(len(self.run.call_args.args[0]), 5)

    def test_extra_stdout_keeps_uncertain_receipt_and_does_not_retry(self):
        self.run.return_value.stdout = "warning\n" + self.run.return_value.stdout
        self.assertEqual(self.send(auto_enter=True), ("UNKNOWN", None))
        self.assertEqual(self.send(auto_enter=True), ("UNKNOWN", None))
        self.assertEqual(self.run.call_count, 1)

    def test_receipt_write_failure_retains_intent_and_does_not_retry(self):
        self.queue.db.execute("""CREATE TRIGGER reject_receipt BEFORE UPDATE ON codex_queue_receipts
            BEGIN SELECT RAISE(ABORT, 'receipt write failure'); END""")
        self.queue.db.commit()
        with self.assertRaises(sqlite3.Error):
            self.send(auto_enter=True)
        self.queue.db.rollback()
        self.queue = self.open()
        self.assertEqual(self.send(auto_enter=True), ("UNKNOWN", None))
        self.assertEqual(self.run.call_count, 1)

    def test_same_notification_not_repeated_after_reopen(self):
        self.send(auto_enter=True)
        self.queue = self.open()
        self.assertEqual(self.send(auto_enter=True), ("QUEUED", SUBMISSION))
        self.assertEqual(self.run.call_count, 1)

    def test_timeout_not_retried_after_reopen(self):
        self.run.side_effect = subprocess.TimeoutExpired("codex", 15)
        self.assertEqual(self.send(auto_enter=True), ("UNKNOWN", None))
        self.queue = self.open()
        self.assertEqual(self.send(auto_enter=True), ("UNKNOWN", None))
        self.assertEqual(self.run.call_count, 1)

    def test_crash_between_intent_and_receipt_does_not_retry(self):
        self.queue.db.execute("INSERT INTO codex_queue_receipts VALUES (?, ?, 'PENDING', NULL)",
                              (THREAD, "hu:mail-42"))
        self.queue.db.commit()
        self.assertEqual(self.send(auto_enter=True), ("UNKNOWN", None))
        self.run.assert_not_called()

    def test_wrong_thread_or_bad_receipt_is_unknown(self):
        for index, stdout in enumerate(("ok", f"Queued message {SUBMISSION} for thread {OTHER}.",
                                       f"Queued message {'-' * 36} for thread {THREAD}.")):
            self.run.return_value.stdout = stdout
            self.assertEqual(self.queue.deliver(THREAD, str(index), "notice", auto_enter=True),
                             ("UNKNOWN", None))

    def test_nonzero_even_with_receipt_is_unknown(self):
        self.run.return_value.returncode = 1
        self.assertEqual(self.send(auto_enter=True), ("UNKNOWN", None))

    def test_receipt_is_scoped_to_thread(self):
        self.send(auto_enter=True)
        self.run.return_value.stdout = f"Queued message {SUBMISSION} for thread {OTHER}."
        self.assertEqual(self.queue.deliver(OTHER, "hu:mail-42", "notice", auto_enter=True),
                         ("QUEUED", SUBMISSION))
        self.assertEqual(self.run.call_count, 2)


class CaptureTests(unittest.TestCase):
    def probe(self, args=None, owner=501, executable=None, changed=False):
        binary = "/users/test/.codex/packages/standalone/releases/0.162.1/bin/codex"
        daemon = (owner, 10, 20, 0, "??", args or [binary, "app-server", "--listen",
            "unix://", "--analytics-default-enabled", "--managed-daemon"])
        rows = {30: (501, 20, 30, 0, "??", ["python"]), 20: daemon}
        reads = []
        def inspect(pid):
            reads.append(pid)
            if changed and reads.count(20) > 1:
                return None
            return rows.get(pid)
        with patch.dict(os.environ, {"CODEX_THREAD_ID": THREAD}), \
             patch("os.getpid", return_value=30), patch("os.getuid", return_value=501), \
             patch("os.getsid", side_effect=lambda p: 30 if p in (0, 30) else 20), \
             patch("pwd.getpwuid", return_value=SimpleNamespace(pw_dir="/users/test")), \
             patch("tabus.terminal_frontend.process_info", side_effect=inspect), \
             patch("tabus.terminal_frontend.native_executable", return_value=executable or binary):
            return capture_current_thread()

    def test_managed_daemon_uses_thread_not_parent_tty(self):
        self.assertEqual(self.probe(), THREAD)

    def test_other_user_binary_or_changed_process_rejected(self):
        self.assertIsNone(self.probe(owner=502))
        self.assertIsNone(self.probe(executable="/tmp/codex"))
        self.assertIsNone(self.probe(changed=True))

    def test_unknown_server_modes_rejected(self):
        for args in (["codex", "app-server"], ["codex", "exec"],
                     ["codex", "app-server", "--listen", "ws://127.0.0.1:4500",
                      "--analytics-default-enabled", "--managed-daemon"]):
            self.assertIsNone(self.probe(args=args))


class DoorbellTests(unittest.TestCase):
    def test_codex_route_dispatch_has_stable_identity_and_no_terminal_write(self):
        from tabus import doorbell
        with patch.object(doorbell, "unread_senders", return_value=[{"sender":"jack", "count":1}]), \
             patch.object(doorbell, "route_for", return_value=("codex-queue", THREAD, True)), \
             patch("tabus.codex_queue.deliver_notification", return_value=("QUEUED", 1)) as send, \
             patch.object(doorbell, "send_to_iterm_session") as iterm:
            self.assertEqual(doorbell.deliver_doorbell("hu", 1, "hu:42"),
                             ("QUEUED", "codex-queue", 1))
            self.assertEqual(send.call_args.args[0:2], (THREAD, "hu:42"))
            self.assertIs(send.call_args.args[3], True)
            iterm.assert_not_called()

    def test_queue_acceptance_suppresses_but_unknown_does_not(self):
        from tabus.doorbell_poll import SUPPRESSING
        self.assertIn("QUEUED", SUPPRESSING)
        self.assertNotIn("UNKNOWN", SUPPRESSING)


class RegistrationTests(unittest.TestCase):
    def invoke(self, **overrides):
        from tabus import cli
        args = dict(node="hu", kind="codex", codex_queue=True, auto_enter="on",
                    program=None, take_route=False)
        args.update(overrides)
        with patch.object(cli, "_user_email", return_value=None), \
             patch.object(cli, "_nodekey", return_value=SimpleNamespace(public_key_b58=lambda _: "public")), \
             patch.object(cli, "_node_key_path", return_value="unused"), \
             patch.object(cli, "_capture_route", return_value=(None, None)), \
             patch.object(cli, "_host_id", return_value="host-a"), \
             patch("tabus.codex_queue.capture_current_thread", return_value=THREAD), \
             patch.object(cli, "call", return_value=(200, {"ok":True, "auto_enter":True,
                  "msg":"registered hu route=codex-queue"})) as call, \
             contextlib.redirect_stdout(io.StringIO()):
            try:
                cli.fn_register(SimpleNamespace(**args))
            except SystemExit as exc:
                return exc.code, call
            return 0, call

    def test_explicit_queue_registration_uses_thread_target(self):
        code, call = self.invoke()
        self.assertEqual(code, 0)
        payload = call.call_args.args[2]
        self.assertEqual(payload["adapter"], "codex-queue")
        self.assertEqual(payload["target"], THREAD)
        self.assertIs(payload["auto_enter"], True)
        self.assertEqual(payload["host_id"], "host-a")

    def test_invalid_combinations_never_send_registration(self):
        for overrides in ({"kind":"claude"}, {"auto_enter":"off"},
                          {"auto_enter":None}, {"program":""}, {"take_route":True}):
            code, call = self.invoke(**overrides)
            self.assertEqual(code, 2)
            call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
