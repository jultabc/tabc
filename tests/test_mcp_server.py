"""MCP adapter tests against an isolated SQLite database and loopback daemon."""
import http.client
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import urllib.error
import uuid
from unittest.mock import MagicMock, patch

STATE = tempfile.TemporaryDirectory(prefix="tabc-mcp-test-")
os.environ["TABC_HOME"] = STATE.name
os.environ["TABC_DB"] = str(Path(STATE.name, "test.db"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tabus import daemon, mcp_server as adapter, nodekey


class AdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        daemon.init_extras()
        daemon.HOLD_SEC = 0
        cls.server = daemon.ThreadingHTTPServer(("127.0.0.1", 0), daemon.BusHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        adapter.BASE = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        STATE.cleanup()

    def setUp(self):
        suffix = uuid.uuid4().hex[:10]
        self.alice, self.bob = "alice-" + suffix, "bob-" + suffix
        for node in (self.alice, self.bob):
            adapter.NODE = node
            result = adapter._request("POST", "/register", {
                "node": node, "kind": "generic",
                "pubkey": nodekey.public_key_b58(nodekey.key_path(node))})
            self.assertTrue(result.get("ok"), result)
        adapter.NODE = self.alice

    def test_dm_roundtrip_read_before_send_and_exact_body(self):
        body = "No framing. Quotes ' and | remain unchanged.\nSecond line."
        sent = adapter.tabc_send(self.bob, "A | B's result", body)
        mid = sent["id"]
        self.assertEqual(mid, sent["request_id"])
        self.assertNotIn("body", adapter.tabc_sent()["messages"][0])
        self.assertEqual(adapter.tabc_sent(message_id=mid)["messages"][0]["body"], body)
        adapter.NODE = self.bob
        unread = adapter.tabc_dm()["unread"]
        self.assertEqual(unread[0]["message_id"], mid)
        self.assertEqual(adapter.tabc_send(self.alice, "reply", "blocked")["status"], 400)
        self.assertFalse(adapter.tabc_ack(mid)["ok"])
        self.assertEqual(adapter.tabc_pull()["messages"][0]["body"], body)
        self.assertEqual(adapter.tabc_open(mid)["state"], "INJECTED")
        self.assertTrue(adapter.tabc_ack(mid)["ok"])
        self.assertTrue(adapter.tabc_ack(mid, "PROCESSED")["ok"])
        self.assertIn("id", adapter.tabc_send(self.alice, "reply", "reviewed"))

    def test_claimed_recovery_and_empty_pull_are_separate(self):
        mid = adapter.tabc_send(self.bob, "recovery", "body")["id"]
        adapter.NODE = self.bob
        self.assertEqual(len(adapter.tabc_pull()["messages"]), 1)
        self.assertEqual(adapter.tabc_pull()["messages"], [])
        self.assertEqual(adapter.tabc_dm()["unread"][0]["message_id"], mid)
        self.assertEqual(adapter.tabc_open(mid)["state"], "INJECTED")
        self.assertEqual(adapter.tabc_open(mid)["state"], "INJECTED")

    def test_no_inbox_or_sender_impersonation(self):
        self.assertEqual(adapter._request("GET", "/mailbox",
                         params={"node": self.bob})["status"], 403)
        self.assertEqual(adapter._request("POST", "/send", {
            "from": self.bob, "to": [self.alice], "subject": "x", "body": "x"
        })["status"], 403)

    def test_tac_view_is_read_only_and_send_gate_is_enforced(self):
        name = "topic-" + uuid.uuid4().hex
        self.assertTrue(adapter._request("POST", "/tac_create",
                        {"tac": name, "by": self.alice})["ok"])
        tac = [item["tac_id"] for item in adapter.tabc_tacs()["tacs"]
               if item.get("name") == name][0]
        for node in (self.alice, self.bob):
            self.assertTrue(adapter._request("POST", "/tac_add",
                            {"tac": tac, "node": node, "by": self.alice})["ok"])
        self.assertIn(tac, [item["tac_id"] for item in adapter.tabc_tacs()["tacs"]])
        mid = adapter.tabc_tac_send(tac, "review", "please review")["id"]
        adapter.NODE = self.bob
        self.assertTrue(adapter.tabc_tac_messages(tac)["exists"])
        self.assertEqual(adapter.tabc_tac_send(tac, "reply", "blocked")["status"], 400)
        self.assertEqual(adapter.tabc_open(mid)["state"], "INJECTED")
        self.assertIn("id", adapter.tabc_tac_send(tac, "reply", "opened"))

    def test_send_retry_uses_same_id(self):
        mid = str(uuid.uuid4())
        first = adapter.tabc_send(self.bob, "retry", "same", message_id=mid)
        second = adapter.tabc_send(self.bob, "retry", "same", message_id=mid)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(adapter.tabc_sent()["messages"]), 1)

    def test_missing_identity_and_key_never_generate_keys(self):
        with patch.object(adapter, "NODE", ""):
            self.assertEqual(adapter.tabc_who()["error"], "NO_NODE")
        with patch.object(adapter, "NODE", "unregistered"):
            self.assertEqual(adapter.tabc_who()["error"], "NO_NODE_KEY")
            self.assertFalse(Path(nodekey.key_path("unregistered")).exists())
        with patch.object(adapter, "NODE", "../unsafe"):
            self.assertEqual(adapter.tabc_who()["error"], "BAD_NODE_KEY")

    def test_bad_base_rejected_before_signing(self):
        for base in ("https://example.com", "file:///tmp/example", "http://localhost/path",
                     "http://u:p@localhost", "http://localhost?x=1", "http://localhost:bad",
                     "http://localhost/#fragment"):
            with self.subTest(base=base), patch.object(adapter, "BASE", base), \
                    patch.object(nodekey, "sign") as sign:
                self.assertEqual(adapter.tabc_who()["error"], "BAD_BASE")
                sign.assert_not_called()

    def test_redirect_is_not_followed(self):
        self.assertIsNone(adapter._NoRedirect().redirect_request(
            None, None, 302, "redirect", {}, "https://example.com"))

    def test_limits_and_identifiers_fail_before_requests(self):
        with patch.object(adapter, "_request") as request:
            for limit in (0, -1, 201, True):
                with self.assertRaises(ValueError):
                    adapter.tabc_dm(limit)
            with self.assertRaises(ValueError):
                adapter.tabc_pull(51)
            for call in (lambda: adapter.tabc_open("prefix"),
                         lambda: adapter.tabc_ack(str(uuid.uuid4()), "INJECTED"),
                         lambda: adapter.tabc_send("", "subject", "body")):
                with self.assertRaises(ValueError):
                    call()
            request.assert_not_called()

    def test_write_connection_loss_is_unknown_and_not_retried(self):
        for error in (TimeoutError(), http.client.RemoteDisconnected(),
                      ConnectionResetError(), urllib.error.URLError("reset"),
                      http.client.IncompleteRead(b"partial")):
            with self.subTest(error=type(error).__name__), \
                    patch.object(adapter._opener, "open", side_effect=error) as request:
                result = adapter.tabc_send(self.bob, "lost", "response")
                self.assertEqual(result["error"], "UNKNOWN")
                self.assertFalse(result["retry_performed"])
                uuid.UUID(result["request_id"])
                request.assert_called_once()

    def test_http_refusal_and_unknown_are_distinct(self):
        for code in (302, 400, 401, 403, 409, 408, 500, 503):
            error = urllib.error.HTTPError(adapter.BASE, code, "test", {}, io.BytesIO(b"refused"))
            with self.subTest(code=code), patch.object(adapter._opener, "open", side_effect=error):
                result = adapter.tabc_send(self.bob, "error", "test")
                # 🔴 A 408 without a code came from something between here and tabd, which says
                #    nothing about what was stored. tabd's own 408 carries a code (below).
                self.assertEqual(result["error"], "UNKNOWN" if code >= 500 or code == 408 else "HTTP_ERROR")

    def test_408_is_only_definite_with_a_readable_top_level_code(self):
        # 🔴 Counterexamples from the independent review: a code somewhere in the text is not
        #    tabd's coded refusal, and a body that cannot be read leaves the request unknown.
        class Unreadable(io.BytesIO):
            def read(self, *a):
                raise http.client.IncompleteRead(b"half")

        bodies = [b'[{"code": "REQUEST_TIMEOUT"}]', b'{"detail": {"code": "REQUEST_TIMEOUT"}}',
                  b"<html><body>code REQUEST_TIMEOUT</body></html>", b'"code"', b""]
        for body in bodies:
            error = urllib.error.HTTPError(adapter.BASE, 408, "test", {}, io.BytesIO(body))
            with self.subTest(body=body[:24]), patch.object(adapter._opener, "open", side_effect=error):
                result = adapter.tabc_send(self.bob, "error", "test")
            self.assertEqual(result["error"], "UNKNOWN", body[:24])
        error = urllib.error.HTTPError(adapter.BASE, 408, "test", {}, Unreadable(b"half"))
        with patch.object(adapter._opener, "open", side_effect=error):
            result = adapter.tabc_send(self.bob, "error", "test")
        self.assertEqual(result["error"], "UNKNOWN")
        self.assertIn("could not be read", result["detail"])
        # Any status whose body could not be read is unknown for a write, not a definite failure.
        error = urllib.error.HTTPError(adapter.BASE, 400, "test", {}, Unreadable(b"half"))
        with patch.object(adapter._opener, "open", side_effect=error):
            result = adapter.tabc_send(self.bob, "error", "test")
        self.assertEqual((result["error"], result["status"]), ("UNKNOWN", 400))
        # A read-only tool says the request failed rather than claiming an unknown write.
        error = urllib.error.HTTPError(adapter.BASE, 400, "test", {}, Unreadable(b"half"))
        with patch.object(adapter._opener, "open", side_effect=error):
            result = adapter.tabc_tacs()
        self.assertEqual(result["error"], "REQUEST_FAILED")

    def test_coded_408_is_a_definite_failure(self):
        body = json.dumps({"error": "request body took too long", "code": "REQUEST_TIMEOUT",
                           "message": "the request body did not arrive within 30 seconds",
                           "details": {"limit": 30, "unit": "seconds"}, "retry": "as_is"}).encode()
        error = urllib.error.HTTPError(adapter.BASE, 408, "test", {}, io.BytesIO(body))
        with patch.object(adapter._opener, "open", side_effect=error):
            result = adapter.tabc_send(self.bob, "error", "test")
        self.assertEqual((result["error"], result["status"]), ("HTTP_ERROR", 408))
        self.assertEqual((result["code"], result["retry"]), ("REQUEST_TIMEOUT", "as_is"))
        self.assertEqual(result["details"], {"limit": 30, "unit": "seconds"})

    def test_coded_refusal_is_kept_whole(self):
        result = adapter.tabc_send(self.bob, "large", "a" * 70000)
        self.assertEqual((result["error"], result["status"]), ("HTTP_ERROR", 400))
        self.assertEqual((result["code"], result["retry"]), ("BODY_TOO_LARGE", "never"))
        self.assertEqual(result["details"], {"field": "body", "bytes": 70000, "limit": 65536,
                                             "unit": "utf8_bytes"})
        self.assertEqual(json.loads(result["detail"])["error"], "body is missing or too large")

    def test_blank_body_and_subject_are_judged_by_tabd(self):
        for body in ("", " \t\n", "\u3000"):
            result = adapter.tabc_send(self.bob, "blank", body)
            self.assertEqual((result["status"], result["code"]), (400, "BODY_EMPTY"), repr(body))
        for subject in ("", " ", "\u3000"):
            result = adapter.tabc_send(self.bob, subject, "body")
            self.assertEqual((result["status"], result["code"]), (400, "SUBJECT_EMPTY"), repr(subject))
        self.assertEqual(adapter.tabc_sent()["messages"], [])

    def test_long_refusal_is_not_cut(self):
        refusal = {"error": "read their messages first: " + ", ".join(f"n{i:03d} (1)" for i in range(400)),
                   "code": "UNREAD_BLOCKED", "message": "open them",
                   "details": {"scope": "dm", "recipients": [{"node": f"n{i:03d}", "unread": 1}
                                                            for i in range(400)]},
                   "retry": "after_condition", "ignored": "not lifted"}
        raw = json.dumps(refusal).encode()
        self.assertGreater(len(raw), 2048 * 4)
        for body, lifted in ((raw, True), (b"plain refusal " * 400, False)):
            error = urllib.error.HTTPError(adapter.BASE, 400, "test", {}, io.BytesIO(body))
            with patch.object(adapter._opener, "open", side_effect=error):
                result = adapter.tabc_send(self.bob, "long", "refusal")
            self.assertEqual(result["detail"], body.decode())
            self.assertNotIn("ignored", result)
            if lifted:
                self.assertEqual(result["details"], refusal["details"])
                self.assertEqual((result["code"], result["retry"]), ("UNREAD_BLOCKED", "after_condition"))
            else:
                self.assertNotIn("code", result)

    def test_bad_write_responses_are_unknown(self):
        for body in (b"not json", b"[]", b"{}", b'{"id":"unexpected-id"}'):
            response = MagicMock()
            response.__enter__.return_value = response
            response.read.return_value = body
            with patch.object(adapter._opener, "open", return_value=response):
                self.assertEqual(adapter.tabc_send(self.bob, "x", "y")["error"], "UNKNOWN")

    def test_key_disappearing_before_signing_is_not_recreated(self):
        with patch("builtins.open", side_effect=FileNotFoundError()), \
                patch.object(nodekey, "ensure_key") as create:
            self.assertEqual(adapter.tabc_who()["error"], "BAD_NODE_KEY")
            create.assert_not_called()

    def test_unreadable_refusal_body_is_unknown_and_keeps_status_and_request_id(self):
        # 🔴 The refusal could not be read, so a write is UNKNOWN rather than a definite failure:
        #    a caller that resends with a new id would store the message twice.
        error = urllib.error.HTTPError(adapter.BASE, 403, "test", {}, None)
        error.read = MagicMock(side_effect=http.client.IncompleteRead(b"partial"))
        with patch.object(adapter._opener, "open", side_effect=error):
            result = adapter.tabc_send(self.bob, "x", "y")
            self.assertEqual((result["error"], result["status"]), ("UNKNOWN", 403))
            uuid.UUID(result["request_id"])

    def test_optional_sdk_and_python_version_have_clear_errors(self):
        with patch.dict(sys.modules, {"mcp.server.fastmcp": None}):
            with self.assertRaisesRegex(RuntimeError, "Install the MCP extra"):
                adapter.create_server()
        with patch.object(sys, "version_info", (3, 9)):
            with self.assertRaisesRegex(RuntimeError, "Python 3.10"):
                adapter.create_server()

    def test_partial_open_does_not_claim_success_or_known_state(self):
        mid = str(uuid.uuid4())
        with patch.object(adapter, "_request", side_effect=[
            {"ok": True, "id": mid, "body": "data", "state": "CLAIMED"},
            {"ok": False, "error": "UNKNOWN"}
        ]):
            result = adapter.tabc_open(mid)
        self.assertFalse(result["ok"])
        self.assertFalse(result["injected_recorded"])
        self.assertIsNone(result["state"])
        self.assertEqual(result["body"], "data")


if __name__ == "__main__":
    unittest.main()
