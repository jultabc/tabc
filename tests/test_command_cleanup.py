"""CLI routing checks; no live requests or operator state changes."""
import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tabus import admin, cli


class Commands(unittest.TestCase):
    def test_old_commands_rejected(self):
        for name in ("name", "mailbox", "config", "rm", "restore"):
            with self.subTest(name=name), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    cli.build_parser().parse_args([name])

    def test_dm_never_sends(self):
        args = cli.build_parser().parse_args(["dm", "--node", "alice"])
        with patch.object(cli, "call", return_value=(200, {"unread": []})) as call:
            with contextlib.redirect_stdout(io.StringIO()):
                cli.COMMANDS[args.cmd][0](args)
        self.assertEqual(call.call_args.args[0], "GET")
        self.assertEqual(call.call_args.kwargs["node"], "alice")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["dm", "--sender", "alice", "--to", "bob", "--subject", "s"])

    def test_admin_identity_and_purge(self):
        for command in ("disable", "enable", "purge"):
            flags = [command, "--node", "alice", "--target", "bob"]
            if command != "enable":
                flags.append("--yes")
            a = admin.build_parser().parse_args(flags)
            with patch.object(cli, "_require_acting_node", side_effect=lambda node: node), \
                 patch.object(cli, "_user_email", return_value=""), \
                 patch.object(cli, "call", return_value=(200, {"ok": True, "msg": "ok"})) as call, \
                 contextlib.redirect_stdout(io.StringIO()):
                admin.commands()[a.cmd][0](a)
            self.assertEqual(call.call_args.kwargs["node"], "alice")
            self.assertEqual(call.call_args.args[2]["node"], "bob")
            if command != "enable":
                self.assertEqual(call.call_args.args[2]["purge"], command == "purge")

    def test_confirmation_before_request(self):
        for command in ("disable", "purge"):
            a = admin.build_parser().parse_args([command, "--node", "alice", "--target", "bob"])
            with patch.object(cli, "call") as call, contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit):
                    admin.commands()[a.cmd][0](a)
                call.assert_not_called()

    def test_sent_is_read_only(self):
        a = cli.build_parser().parse_args(["sent", "--node", "alice"])
        with patch.object(cli, "call", return_value=(200, {"messages": []})) as call, contextlib.redirect_stdout(io.StringIO()):
            cli.fn_sent(a)
        self.assertEqual(call.call_args.args[0], "GET")
        self.assertEqual(call.call_args.kwargs["node"], "alice")

    def test_sent_uses_lowercase_labels(self):
        args = cli.build_parser().parse_args(["sent", "--node", "alice"])
        messages = [
            {"id": "one", "tac_id": None, "accepted_at": "now", "subject": "DM original", "deliveries": []},
            {"id": "two", "tac_id": "Planning", "accepted_at": "now", "subject": "TAC original", "deliveries": []},
        ]
        output = io.StringIO()
        with patch.object(cli, "call", return_value=(200, {"messages": messages})), contextlib.redirect_stdout(output):
            cli.fn_sent(args)
        self.assertIn("[dm] now DM original", output.getvalue())
        self.assertIn("[tac Planning] now TAC original", output.getvalue())


if __name__ == "__main__":
    unittest.main()
