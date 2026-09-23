#!/usr/bin/env python3
"""Send refusals: size limits, payload checks and codes (temp state, own HTTP server).

Pins:
- check_send_payload: type first (body, then subject), then body missing, empty
  (str.isspace), too large (65,536 UTF-8 bytes), then subject too large (1,024).
  null body is BODY_MISSING; null subject is FIELD_TYPE_INVALID. No normalization.
- A refusal is the old sentence: `body is missing or too large` stays for missing
  and oversized bodies, and the unread-block sentences are unchanged.
- bus_send writes nothing and rings nothing on a refusal; payload checks run before
  any state lookup; refusals outside the first code list carry no code.
- tabd answers HTTP 400 with `error` plus code, message, details and retry; a body
  that is not a string is refused instead of dropping the connection; an omitted
  subject is still stored as "".
- `tabc send` keeps `failed: <sentence>` as its first line and adds the code below.
- A declared request length over 524,288 bytes is refused before the signature check.
  The body is read and discarded up to DRAIN_MAX_BYTES within DRAIN_SEC, so a client
  that sends it whole gets the 413. Over DRAIN_MAX_BYTES nothing is read.
- Every socket read waits at most the handler timeout (idle). A long-poll hold is not a
  read, and a response write has no time limit.
- The whole body of an accepted request must arrive within BODY_READ_SEC (monotonic), or the
  answer is 408 REQUEST_TIMEOUT and nothing is stored.
- A body that ends before Content-Length is 400 REQUEST_INCOMPLETE: the signature is verified
  over the bytes that arrived, so it does not catch a request that declares more than it sends.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="send_refusal_")
os.environ["TABC_DB"] = os.path.join(TMP, "t.db")
os.environ["TABC_HOME"] = TMP  # keys and state stay off the real ~/.tabc
os.environ.pop("TABC_NODE", None)
sys.path.insert(0, ROOT)

import tabus  # noqa: E402
from tabus import bus, daemon as tabd, nodekey  # noqa: E402
from tabus import send_refusal as sr  # noqa: E402

assert nodekey.key_path("probe").startswith(TMP), nodekey.key_path("probe")

RINGS = []
bus.ring_doorbell = lambda *a, **k: (RINGS.append(a) or (True, ""))
bus.mirror_iterm = lambda con: []
bus.mirror_claude_sessions = lambda con, skip=(): []

# 🔴 Pinned, not derived from str.isspace: if a Python release changes the set, the
#    check below fails instead of the expectations moving with the implementation.
#    Same 29 code points as the spec (sha256 of the comma-joined %04X list: 3c2c964c6580).
BLANK_CODEPOINTS = (
    0x0009, 0x000A, 0x000B, 0x000C, 0x000D, 0x001C, 0x001D, 0x001E,
    0x001F, 0x0020, 0x0085, 0x00A0, 0x1680, 0x2000, 0x2001, 0x2002,
    0x2003, 0x2004, 0x2005, 0x2006, 0x2007, 0x2008, 0x2009, 0x200A,
    0x2028, 0x2029, 0x202F, 0x205F, 0x3000,
)
BLANK = [chr(c) for c in BLANK_CODEPOINTS]
SURROGATE = chr(0xD800)  # unpaired; JSON can carry it, UTF-8 cannot encode it
NOT_BLANK = ["\u200b", "\ufeff", "\u200f", "\u202e", "\u00a0\u200b", "."]


def exact(unit, size):
    """`unit` repeated, padded with `a` to exactly `size` UTF-8 bytes."""
    s = unit * (size // len(unit.encode()))
    return s + "a" * (size - len(s.encode()))


class PayloadCheck(unittest.TestCase):
    def code(self, subject, body):
        r = sr.check_send_payload(subject, body)
        return None if r is None else r.code

    def test_types_first_body_then_subject(self):
        for value, name in ((1, "number"), (1.5, "number"), ([], "array"), ({}, "object"), (True, "boolean")):
            r = sr.check_send_payload("s", value)
            self.assertEqual((r.code, r.details), ("FIELD_TYPE_INVALID", {"field": "body", "received": name}))
            self.assertEqual(r.retry, "never")
        for value, name in ((None, "null"), (1, "number"), ([], "array")):
            r = sr.check_send_payload(value, "b")
            self.assertEqual((r.code, r.details), ("FIELD_TYPE_INVALID", {"field": "subject", "received": name}))
        self.assertEqual(sr.check_send_payload("x" * 2000, []).details["field"], "body")
        self.assertEqual(sr.check_send_payload(None, "").details["field"], "subject")

    def test_encoding_after_type_before_everything_else(self):
        for bad in (SURROGATE + " x", "x " + SURROGATE, "a" * 500 + SURROGATE + "a" * 500):
            for field, subject, body in (("body", "s", bad), ("subject", bad, "b")):
                r = sr.check_send_payload(subject, body)
                self.assertEqual((r.code, r.details, r.retry), ("FIELD_ENCODING_INVALID", {"field": field}, "never"))
                self.assertEqual(str(r), f"{field} is not valid UTF-8 text")
        self.assertEqual(self.code(SURROGATE, []), "FIELD_TYPE_INVALID")
        self.assertEqual(self.code(SURROGATE, None), "FIELD_ENCODING_INVALID")
        self.assertEqual(self.code("x" * 2000, SURROGATE), "FIELD_ENCODING_INVALID")
        self.assertEqual(self.code(SURROGATE, "a" * 70000), "FIELD_ENCODING_INVALID")
        self.assertIsNone(self.code("s", chr(0x1F600)))  # a paired (valid) supplementary character

    def test_encoding_of_other_send_fields(self):
        for field, value in (("to", ["ok", "x" + SURROGATE]), ("tac", SURROGATE), ("priority", SURROGATE),
                             ("message_id", SURROGATE), ("reply_to", SURROGATE), ("thread_id", SURROGATE),
                             ("expires_at", SURROGATE), ("from", SURROGATE)):
            r = sr.check_send_payload("s", "b", {field: value})
            self.assertEqual((r.code, r.details), ("FIELD_ENCODING_INVALID", {"field": field}), field)
        self.assertIsNone(sr.check_send_payload("s", "b", {"to": ["a", 7, None], "tac": None, "priority": "next"}))
        self.assertEqual(sr.check_send_payload("s", SURROGATE, {"to": [SURROGATE]}).details, {"field": "body"})
        self.assertEqual(sr.check_send_payload([], "b", {"to": [SURROGATE]}).code, "FIELD_TYPE_INVALID")
        self.assertEqual(sr.check_send_payload("s", None, {"to": [SURROGATE]}).code, "FIELD_ENCODING_INVALID")

    def test_subject_empty_after_body_reasons(self):
        for subject in [""] + BLANK + [" \t\n"]:
            r = sr.check_send_payload(subject, "b")
            self.assertEqual((r.code, r.details, r.retry), ("SUBJECT_EMPTY", {"field": "subject"}, "never"), repr(subject))
            self.assertEqual(str(r), "subject is empty or whitespace only")
        for subject in NOT_BLANK:
            self.assertIsNone(self.code(subject, "b"), repr(subject))
        self.assertEqual(self.code("", ""), "BODY_EMPTY")
        self.assertEqual(self.code(" ", "a" * 65537), "BODY_TOO_LARGE")
        self.assertEqual(self.code("", None), "BODY_MISSING")

    def test_missing_body_keeps_the_old_sentence(self):
        r = sr.check_send_payload("s", None)
        self.assertEqual((r.code, str(r)), ("BODY_MISSING", "body is missing or too large"))
        self.assertIsInstance(r, str)

    def test_blank_is_str_isspace(self):
        self.assertEqual([c for c in range(0x110000) if chr(c).isspace()], list(BLANK_CODEPOINTS))
        for ch in [""] + BLANK + [" \t\n", "\u00a0\u3000\u2028\x85", "\x1c\x1d\x1e\x1f"]:
            self.assertEqual(self.code("s", ch), "BODY_EMPTY", repr(ch))
        for ch in NOT_BLANK:
            self.assertIsNone(self.code("s", ch), repr(ch))

    def test_body_limit_in_utf8_bytes(self):
        for unit in ("a", "\u00e9", "\ud55c", "\U0002000b", "\U0001f44d", "a\u0308", "\u0628\u064e"):
            self.assertIsNone(self.code("s", exact(unit, 65536)), repr(unit))
            r = sr.check_send_payload("s", exact(unit, 65537))
            self.assertEqual(r.code, "BODY_TOO_LARGE")
            self.assertEqual(r.details, {"field": "body", "bytes": 65537, "limit": 65536, "unit": "utf8_bytes"})
            self.assertEqual(str(r), "body is missing or too large")

    def test_subject_limit_in_utf8_bytes(self):
        for unit in ("a", "\ud55c", "\U0001f468\u200d\U0001f469\u200d\U0001f467"):
            self.assertIsNone(self.code(exact(unit, 1024), "b"), repr(unit))
            r = sr.check_send_payload(exact(unit, 1025), "b")
            self.assertEqual((r.code, r.details["bytes"], r.details["limit"]), ("SUBJECT_TOO_LARGE", 1025, 1024))

    def test_body_reasons_before_subject_size(self):
        self.assertEqual(self.code("x" * 1025, "a" * 65537), "BODY_TOO_LARGE")
        self.assertEqual(self.code("x" * 1025, " "), "BODY_EMPTY")
        self.assertEqual(self.code("x" * 1025, None), "BODY_MISSING")

    def test_no_normalization(self):
        nfd = unicodedata.normalize("NFD", "\ud55c")
        body = nfd * 7282  # 9 bytes each: 65,538 bytes; the same text in NFC is 21,846 bytes
        r = sr.check_send_payload("s", body)
        self.assertEqual((r.code, r.details["bytes"]), ("BODY_TOO_LARGE", 65538))
        self.assertIsNone(sr.check_send_payload("s", unicodedata.normalize("NFC", body)))

    def test_limits_stay_importable(self):
        self.assertEqual((tabus.MAX_BODY_BYTES, tabus.MAX_SUBJECT_BYTES), (65536, 1024))


class RequestLength(unittest.TestCase):
    def test_declared_length_boundaries(self):
        for ok in (None, "", "0", "524288", " 524288 ", "\t7\t", "007", "0000524288", "0" * 4301, "0" * 4301 + "7"):
            self.assertIsNone(sr.request_length_refusal(ok), repr(ok)[:20])
        for big, reported in (("524289", 524289), ("0000524289", 524289),
                              ("9223372036854775807", 9223372036854775807), ("9223372036854775808", None),
                              ("9" * 20, None), ("9" * 4301, None), ("1" + "0" * 4300, None), ("0" * 4301 + "524289", 524289)):
            status, body = sr.request_length_refusal(big)
            self.assertEqual((status, body["code"], body["retry"], body["error"]),
                             (413, "REQUEST_TOO_LARGE", "never", "request body too large"), big[:20])
            expected = {"limit": 524288, "unit": "bytes"} if reported is None else {"bytes": reported, "limit": 524288, "unit": "bytes"}
            self.assertEqual(body["details"], expected, big[:20])
        for bad in ("-1", "abc", "1e3", "12.5", "0x10", "+12", "1_000", chr(0x0661) + chr(0x0662), "1 2", "-" + "9" * 4301,
                    "12" + chr(0xA0), chr(0xA0) + "12", "12" + chr(0x0B), chr(0x0C) + "12", "12" + chr(0x85), "12\r"):
            status, body = sr.request_length_refusal(bad)
            self.assertEqual((status, body["code"], body["error"]), (400, "REQUEST_LENGTH_INVALID", "invalid Content-Length"), bad[:20])

    def test_repeated_length_headers_must_agree(self):
        for values, expected in ((["5", "5"], (True, 5)), (["5", "05", " 5\t"], (True, 5)), ([], (True, 0)),
                                 (["5", "999"], (False, None)), (["5", ""], (False, None)), (["5", "abc"], (False, None)),
                                 (["9" * 4301, "9" * 4301], (True, None)), (["9" * 4301, "9" * 4300 + "8"], (False, None))):
            self.assertEqual(sr.declared_length(values), expected, [v[:8] for v in values])
        self.assertEqual(sr.request_length_refusal(["600000", "600000"])[1]["details"]["bytes"], 600000)
        self.assertEqual(sr.request_length_refusal(["2", "999"])[1]["code"], "REQUEST_LENGTH_INVALID")
        self.assertIsNone(sr.request_length_refusal(["2", "2"]))

    def test_declared_length_never_converts_more_than_19_digits(self):
        # 🔴 Python 3.10+ refuses int() on more than 4,300 digits; 3.9 does not. Watching the
        #    conversions pins the rule on every version, not only where int() raises.
        seen = []
        real_int = int

        def spy(value, *args):
            seen.append(len(value) if isinstance(value, str) else 0)
            return real_int(value, *args)

        sr.int = spy
        try:
            for raw in ("9" * 4301, "0" * 4301, "0" * 5000 + "12", "1" + "0" * 19, "9223372036854775808", "524289"):
                sr.request_length_refusal(raw)
                sr.declared_length(raw)
        finally:
            del sr.int
        self.assertTrue(seen)
        self.assertLessEqual(max(seen), 19)

    def test_cli_line_names_the_request_when_no_field(self):
        from tabus import cli
        self.assertEqual(cli._refusal_line({"code": "REQUEST_TOO_LARGE", "details": {"bytes": 600000, "limit": 524288}}),
                         "code REQUEST_TOO_LARGE \u00b7 request 600000/524288 bytes")
        self.assertEqual(cli._refusal_line({"code": "REQUEST_LENGTH_INVALID", "details": {}}), "code REQUEST_LENGTH_INVALID")
        self.assertEqual(cli._refusal_line({"code": "REQUEST_TOO_LARGE", "details": {"limit": 524288, "unit": "bytes"}}),
                         "code REQUEST_TOO_LARGE")

    def test_discard_and_idle_values_are_the_test_candidates(self):
        # 🔴 Candidates under test, not confirmed values. A change here is a decision, not a refactor.
        self.assertEqual((tabd.DRAIN_MAX_BYTES, tabd.DRAIN_SEC, tabd.SOCKET_IDLE_SEC, tabd.BusHandler.timeout,
                          tabd.BODY_READ_SEC), (16 * 1024 * 1024, 5, 3, 3, 30))
        self.assertGreater(tabd.BODY_READ_SEC, tabd.SOCKET_IDLE_SEC)
        self.assertGreater(tabd.DRAIN_MAX_BYTES, sr.MAX_REQUEST_BYTES)

    def test_largest_valid_send_fits(self):
        payload = {"from": "n" * 64, "to": ["n" * 64] * 20, "subject": chr(1) * 1024, "body": chr(1) * 65536,
                   "priority": "next", "message_id": "0" * 36}
        size = len(json.dumps(payload, ensure_ascii=False).encode())
        self.assertLess(size, sr.MAX_REQUEST_BYTES)


class BusSend(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.con = tabus.connect()
        cls.con.executescript(tabus.SCHEMA)
        tabus.migrate(cls.con)
        cls.con.commit()
        for n in ("alice", "bob", "carol"):
            tabus.bus_register(cls.con, n, "generic")
        tabus.bus_tac_create(cls.con, "t", by="alice")
        cls.tac = [row["tac_id"] for row in tabus.bus_tac_list(cls.con)
                   if row.get("name") == "t"][0]
        for n in ("alice", "bob", "carol"):
            tabus.bus_tac_add(cls.con, cls.tac, n, "alice")

    @classmethod
    def tearDownClass(cls):
        cls.con.close()

    def rows(self):
        return tuple(self.con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("messages", "deliveries"))

    def test_refusals_write_and_ring_nothing(self):
        cases = (
            (1, "b", "FIELD_TYPE_INVALID"), ("s", [], "FIELD_TYPE_INVALID"), ("s", None, "BODY_MISSING"),
            ("s", "\u3000", "BODY_EMPTY"), ("s", "a" * 65537, "BODY_TOO_LARGE"), ("x" * 1025, "b", "SUBJECT_TOO_LARGE"),
            ("s", SURROGATE, "FIELD_ENCODING_INVALID"), (SURROGATE, "b", "FIELD_ENCODING_INVALID"),
            ("", "b", "SUBJECT_EMPTY"), (" \t", "b", "SUBJECT_EMPTY"),
        )
        for subject, body, code in cases:
            before, rings = self.rows(), len(RINGS)
            mid, info = tabus.bus_send(self.con, "alice", ["bob"], subject, body)
            self.assertIsNone(mid)
            self.assertEqual(info.code, code)
            self.assertEqual((self.rows(), len(RINGS)), (before, rings))

    def test_payload_checks_come_before_state(self):
        mid, info = tabus.bus_send(self.con, "nobody", ["bob"], "s", " ")
        self.assertEqual(info.code, "BODY_EMPTY")
        mid, info = tabus.bus_send(self.con, "alice", [], "s", "", tac_id="no-such-tac")
        self.assertEqual(info.code, "BODY_EMPTY")

    def test_other_refusals_and_tac_codes(self):
        mid, info = tabus.bus_send(self.con, "nobody", ["bob"], "s", "b")
        self.assertEqual(info, "unregistered sender: nobody")
        self.assertFalse(hasattr(info, "code"))
        mid, info = tabus.bus_send(self.con, "alice", [], "s", "b", tac_id="no-such-tac")
        self.assertEqual(info.code, "TAC_ID_INVALID")
        absent = "0b3f1f2e-8a3c-4d5e-9f10-1a2b3c4d5e6f"
        mid, info = tabus.bus_send(self.con, "alice", [], "s", "b", tac_id=absent)
        self.assertEqual(info.code, "TAC_NOT_FOUND")
        self.assertEqual(info.retry, "never")

    def test_boundaries_are_stored_as_sent(self):
        body, subject = exact("\ud55c", 65536), exact("\U0001f44d", 1024)
        mid, _ = tabus.bus_send(self.con, "carol", ["bob"], subject, body)
        row = self.con.execute("SELECT subject, body FROM messages WHERE id=?", (mid,)).fetchone()
        self.assertEqual((row["subject"], row["body"]), (subject, body))
        mid, _ = tabus.bus_send(self.con, "carol", ["bob"], "\u200b", "\u200b")
        row = self.con.execute("SELECT subject, body FROM messages WHERE id=?", (mid,)).fetchone()
        self.assertEqual((row["subject"], row["body"]), ("\u200b", "\u200b"))

    def test_unread_blocked_dm_and_tac(self):
        tabus.bus_send(self.con, "bob", ["alice"], "q", "question")
        mid, info = tabus.bus_send(self.con, "alice", ["bob"], "a", "answer")
        self.assertIsNone(mid)
        self.assertTrue(info.startswith("read their messages first: bob (1)."))
        self.assertEqual((info.code, info.retry), ("UNREAD_BLOCKED", "after_condition"))
        self.assertEqual(info.details, {"scope": "dm", "recipients": [{"node": "bob", "unread": 1}]})
        tabus.bus_send(self.con, "carol", [], "t1", "one", tac_id=self.tac)
        tabus.bus_send(self.con, "carol", [], "t2", "two", tac_id=self.tac)
        mid, info = tabus.bus_send(self.con, "alice", [], "r", "reply", tac_id=self.tac)
        self.assertTrue(info.startswith(f"tac '{self.tac}': 2 unread. Read them first"))
        self.assertEqual(info.details, {"scope": "tac", "tac": self.tac, "unread": 2})


class QuietServer(tabd.ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass


def signed(port, node, path, payload, raw=None):
    body = raw if raw is not None else json.dumps(payload, ensure_ascii=False)
    ts = str(int(time.time()))
    sig = nodekey.b58encode(nodekey.sign(nodekey.canonical_request(node, "POST", path, body, ts), nodekey.key_path(node)))
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body.encode(), method="POST",
                                 headers={"X-Node": node, "X-Node-Ts": ts, "X-Node-Sig": sig,
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        with e:
            return e.code, json.loads(e.read() or b"{}")


def raw_request(port, method, path, length_header, body=b"", timeout=5, end_body=False):
    """Send headers (and optionally a body) as written; return the status line or a failure name.

    end_body: shut the write side after sending, so a daemon discarding the declared body
    sees its end instead of waiting."""
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        head = f"{method} {path} HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
        head += "X-Node: hana\r\nX-Node-Ts: 1\r\nX-Node-Sig: 1\r\n"
        head += f"Content-Length: {length_header}\r\n\r\n"
        s.sendall(head.encode() + body)
        if end_body:
            s.shutdown(socket.SHUT_WR)
        data = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
        return data.split(b"\r\n", 1)[0].decode("latin-1"), data.split(b"\r\n\r\n", 1)[-1]
    except socket.timeout:
        return "timeout", b""
    finally:
        s.close()


class Http(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tabd.init_extras()
        cls.server = QuietServer(("127.0.0.1", 0), tabd.BusHandler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.port = cls.server.server_port
        for n in ("hana", "joon"):
            pub = nodekey.public_key_b58(nodekey.key_path(n))
            status, _ = signed(cls.port, n, "/register", {"node": n, "kind": "generic", "pubkey": pub})
            assert status == 200, status

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def send(self, **payload):
        return signed(self.port, "hana", "/send", {"from": "hana", "to": ["joon"], **payload})

    def test_coded_400(self):
        status, r = self.send(subject="s", body="a" * 70000)
        self.assertEqual(status, 400)
        self.assertEqual(r["error"], "body is missing or too large")
        self.assertEqual((r["code"], r["retry"]), ("BODY_TOO_LARGE", "never"))
        self.assertEqual(r["details"], {"field": "body", "bytes": 70000, "limit": 65536, "unit": "utf8_bytes"})
        self.assertIn("70000", r["message"])

    def test_non_string_body_is_refused_not_dropped(self):
        for value in (7, [1], {"a": 1}, True):
            status, r = self.send(subject="s", body=value)
            self.assertEqual((status, r.get("code")), (400, "FIELD_TYPE_INVALID"), repr(value))
        status, r = self.send(subject=None, body="b")
        self.assertEqual((status, r["details"]["field"]), (400, "subject"))

    def test_unpaired_surrogate_is_refused_not_dropped(self):
        escape = chr(92) + "ud800"  # the JSON escape as text; decodes to an unpaired surrogate
        for field in ("body", "subject"):
            fields = {"subject": "s", "body": "b", field: escape + " x"}
            raw = ('{"from": "hana", "to": ["joon"], "subject": "%s", "body": "%s"}' % (fields["subject"], fields["body"]))
            status, r = signed(self.port, "hana", "/send", None, raw=raw)
            self.assertEqual((status, r.get("code"), r.get("details")), (400, "FIELD_ENCODING_INVALID", {"field": field}))
        for field, fragment in (("to", '"to": ["%s"]' % escape), ("tac", '"to": [], "tac": "%s"' % escape),
                                ("message_id", '"to": ["joon"], "message_id": "%s"' % escape),
                                ("priority", '"to": ["joon"], "priority": "%s"' % escape),
                                ("thread_id", '"to": ["joon"], "thread_id": "%s"' % escape),
                                ("reply_to", '"to": ["joon"], "reply_to": "%s"' % escape),
                                ("expires_at", '"to": ["joon"], "expires_at": "%s"' % escape)):
            raw = '{"from": "hana", %s, "subject": "s", "body": "b"}' % fragment
            status, r = signed(self.port, "hana", "/send", None, raw=raw)
            self.assertEqual((status, r.get("code"), r.get("details")), (400, "FIELD_ENCODING_INVALID", {"field": field}))

    def test_missing_body_and_omitted_subject(self):
        status, r = self.send(subject="s")
        self.assertEqual((status, r["code"], r["error"]), (400, "BODY_MISSING", "body is missing or too large"))
        for payload in ({"body": "no subject"}, {"subject": "", "body": "b"}, {"subject": " ", "body": "b"}):
            status, r = self.send(**payload)
            self.assertEqual((status, r.get("code"), r.get("details")), (400, "SUBJECT_EMPTY", {"field": "subject"}), payload)

    def test_request_length_refused_before_signature(self):
        started = time.time()
        for method, path, length, status in (("POST", "/send", "524289", 413), ("POST", "/send", "-1", 400),
                                             ("POST", "/send", "abc", 400), ("GET", "/who", "600000", 413)):
            line, body = raw_request(self.port, method, path, length, end_body=True)
            self.assertIn(f" {status} ", line + " ", (method, length, line))
            code = json.loads(body)["code"]
            self.assertEqual(code, "REQUEST_TOO_LARGE" if status == 413 else "REQUEST_LENGTH_INVALID")
        self.assertLess(time.time() - started, 10, "a body that ends early is not waited for")
        line, _ = raw_request(self.port, "POST", "/send", "2", b"{}")
        self.assertIn(" 401 ", line + " ", "within the limit the signature check still decides")

    def test_length_with_whitespace_other_than_sp_and_htab_gets_400(self):
        # Header bytes as sent; http.server decodes them as latin-1, so 0xA0 arrives as U+00A0.
        for value in (b"12\xa0", b"\xa012", b"12\x0b", b"12\x85"):
            s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
            try:
                s.sendall(b"POST /send HTTP/1.1\r\nHost: x\r\nX-Node: hana\r\nX-Node-Ts: 1\r\nX-Node-Sig: 1\r\n"
                          b"Content-Length: " + value + b"\r\n\r\n" + b"{}" + b"a" * 10)
                s.shutdown(socket.SHUT_WR)
                data, _, how, first = until_end(s, time.monotonic(), 10)
            finally:
                s.close()
            head, _, body = data.partition(b"\r\n\r\n")
            self.assertTrue(head.startswith(b"HTTP/1.0 400"), (value, how, data[:80]))
            self.assertEqual(json.loads(body)["code"], "REQUEST_LENGTH_INVALID")

    def test_repeated_length_headers_that_differ_get_400(self):
        def post(lengths, body):
            s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
            try:
                head = b"POST /send HTTP/1.1\r\nHost: x\r\nX-Node: hana\r\nX-Node-Ts: 1\r\nX-Node-Sig: 1\r\n"
                head += b"".join(b"Content-Length: " + n + b"\r\n" for n in lengths)
                s.sendall(head + b"\r\n" + body)
                s.shutdown(socket.SHUT_WR)
                data, _, how, first = until_end(s, time.monotonic(), 10)
            finally:
                s.close()
            return data
        data = post((b"2", b"999"), b"{}")
        self.assertTrue(data.startswith(b"HTTP/1.0 400"), data[:80])
        self.assertEqual(json.loads(data.partition(b"\r\n\r\n")[2])["code"], "REQUEST_LENGTH_INVALID")
        data = post((b"2", b"02"), b"{}")
        self.assertTrue(data.startswith(b"HTTP/1.0 401"), (data[:80], "same length twice: the signature check decides"))

    def test_long_digit_lengths_get_an_answer(self):
        # 🔴 4,301 digits raised inside the handler: no response at all (jiso review, Python 3.11.6).
        for length, status, details in (("9" * 4301, 413, {"limit": 524288, "unit": "bytes"}),
                                        ("0" * 4301 + "524289", 413, {"bytes": 524289, "limit": 524288, "unit": "bytes"})):
            line, raw = raw_request(self.port, "POST", "/send", length, end_body=True)
            self.assertIn(f" {status} ", line + " ", (len(length), line))
            self.assertEqual(json.loads(raw)["details"], details)
        # Accepted as 0 and 2: signed, so the body is read and the request reaches the send check.
        for length, body in (("0" * 4301, ""), ("0" * 4301 + "2", "{}")):
            s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
            try:
                s.sendall(signed_head("hana", "POST", "/send", body, length=length) + body.encode())
                data, _, how, first = until_end(s, time.monotonic(), 10)
            finally:
                s.close()
            self.assertTrue(data.startswith(b"HTTP/1.0 403"), (len(length), how, data[:80]))

    def test_mailbox_and_pull_limit_that_is_not_an_integer_gets_400(self):
        # 🔴 int() outside try raised in the handler: 0 bytes back (jack review J1).
        #    4,301 digits: Python with the int digit limit (3.10+) refuses the conversion, so 400,
        #    as /sent and tac_messages answer. Python 3.9.13 converts it, and the limit is capped.
        # 0 means no limit (PYTHONINTMAXSTRDIGITS=0); a limit above 4,301 converts the value too.
        digit_limit = 0 < getattr(sys, "get_int_max_str_digits", lambda: 0)() < 4301
        saved_hold = tabd.HOLD_SEC
        tabd.HOLD_SEC = 0.5  # a capped /pull holds; keep that short
        try:
            for path in ("/mailbox", "/pull"):
                for limit, refused in (("abc", True), ("9" * 4301, digit_limit)):
                    target = f"{path}?node=hana&limit={limit}"
                    s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
                    try:
                        s.sendall(signed_head("hana", "GET", target, ""))
                        data, seconds, how, first = until_end(s, time.monotonic(), 10)
                    finally:
                        s.close()
                    head, _, body = data.partition(b"\r\n\r\n")
                    status = b"HTTP/1.0 400" if refused else b"HTTP/1.0 200"
                    self.assertTrue(head.startswith(status), (path, len(limit), how, data[:80]))
                    if refused:
                        self.assertEqual(json.loads(body), {"error": "limit must be an integer"})
                        self.assertLess(seconds, 0.5, "refused before the long-poll hold")
        finally:
            tabd.HOLD_SEC = saved_hold

    def test_over_limit_body_sent_whole_gets_the_413(self):
        # 🔴 Refusing without reading, a client still writing the body got a reset instead of the
        #    413: 0/50 at 1 MiB. Up to DRAIN_MAX_BYTES the daemon now reads the body first.
        for size in (sr.MAX_REQUEST_BYTES + 1, 1024 * 1024, tabd.DRAIN_MAX_BYTES):
            for _ in range(3):
                req = urllib.request.Request(f"http://127.0.0.1:{self.port}/send", data=b"a" * size, method="POST",
                                             headers={"X-Node": "hana", "X-Node-Ts": "1", "X-Node-Sig": "1"})
                with self.assertRaises(urllib.error.HTTPError, msg=size) as caught:
                    urllib.request.urlopen(req, timeout=15)
                with caught.exception as e:
                    r = json.loads(e.read())
                self.assertEqual((e.code, r["code"], r["details"]["bytes"]), (413, "REQUEST_TOO_LARGE", size))

    def test_cli_over_limit_request_prints_failed_and_code(self):
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "ITERM_SESSION_ID", "TMUX", "TMUX_PANE")}
        env.update(TABC_BUS_URL=f"http://127.0.0.1:{self.port}", PYTHONPATH=ROOT)
        path = os.path.join(TMP, "over.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("a" * 600000)
        run = subprocess.run([sys.executable, "-m", "tabus.cli", "send", "--sender", "hana", "--to", "joon",
                              "--subject", "s", "--body-file", path],
                             env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
        lines = run.stdout.splitlines()
        self.assertEqual(run.returncode, 1, run.stderr)
        self.assertEqual(lines[0], "failed: request body too large")
        self.assertTrue(lines[1].startswith("  code REQUEST_TOO_LARGE · request "), lines)
        self.assertTrue(lines[1].endswith("/524288 bytes"), lines)
        self.assertGreater(int(lines[1].split(" request ")[1].split("/")[0]), 600000)

    def test_largest_valid_send_is_stored(self):
        status, r = self.send(subject=chr(1) * 1024, body=chr(1) * 65536)
        self.assertEqual(status, 200, r)

    def test_uncoded_refusal_shape_unchanged(self):
        status, r = signed(self.port, "hana", "/send", {"from": "hana", "to": ["ghost"], "subject": "s", "body": "b"})
        self.assertEqual((status, r), (400, {"error": "no valid recipients"}))

    def test_cli_first_line_then_code(self):
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "ITERM_SESSION_ID", "TMUX", "TMUX_PANE")}
        env.update(TABC_BUS_URL=f"http://127.0.0.1:{self.port}", PYTHONPATH=ROOT)
        path = os.path.join(TMP, "big.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\ud55c" * 22000)
        run = subprocess.run([sys.executable, "-m", "tabus.cli", "send", "--sender", "hana", "--to", "joon",
                              "--subject", "s", "--body-file", path],
                             env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
        lines = run.stdout.splitlines()
        self.assertEqual(run.returncode, 1)
        self.assertEqual(lines[0], "failed: body is missing or too large")
        self.assertEqual(lines[1], "  code BODY_TOO_LARGE \u00b7 body 66000/65536 bytes")


def signed_head(node, method, path, body_text, length=None):
    ts = str(int(time.time()))
    sig = nodekey.b58encode(nodekey.sign(nodekey.canonical_request(node, method, path, body_text, ts), nodekey.key_path(node)))
    size = len(body_text.encode()) if length is None else length
    return (f"{method} {path} HTTP/1.1\r\nHost: x\r\nX-Node: {node}\r\nX-Node-Ts: {ts}\r\nX-Node-Sig: {sig}\r\n"
            f"Content-Type: application/json\r\nContent-Length: {size}\r\n\r\n").encode()


def until_end(s, started, limit):
    """Read until the daemon closes.

    Returns (bytes received, seconds to the close, how it ended, seconds to the first byte).
    The two times differ: the daemon keeps discarding what a refused client is still sending
    before it closes, so an answer can arrive well before the close."""
    s.settimeout(limit)
    data = b""
    first = None
    try:
        while True:
            chunk = s.recv(65536)
            if not chunk:
                return data, time.monotonic() - started, "closed", first
            if first is None:
                first = time.monotonic() - started
            data += chunk
    except socket.timeout:
        return data, time.monotonic() - started, "client timeout", first
    except OSError:
        return data, time.monotonic() - started, "reset", first


class SlowPeers(unittest.TestCase):
    """Timing, with small values: socket idle 1 s, discard up to 1 MiB within 2 s, long-poll hold 2.5 s,
    whole body within 2 s."""

    IDLE, DRAIN_SEC, DRAIN_MAX, HOLD, BODY_SEC = 1.0, 2.0, 1024 * 1024, 2.5, 2.0

    @classmethod
    def setUpClass(cls):
        cls.saved = (tabd.DRAIN_SEC, tabd.DRAIN_MAX_BYTES, tabd.HOLD_SEC, tabd.BODY_READ_SEC)
        tabd.BODY_READ_SEC = cls.BODY_SEC
        tabd.DRAIN_SEC, tabd.DRAIN_MAX_BYTES, tabd.HOLD_SEC = cls.DRAIN_SEC, cls.DRAIN_MAX, cls.HOLD
        tabd.init_extras()
        handler = type("QuickIdleHandler", (tabd.BusHandler,), {"timeout": cls.IDLE})
        cls.server = QuietServer(("127.0.0.1", 0), handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.port = cls.server.server_port
        for n in ("mina", "yuna", "sora"):
            pub = nodekey.public_key_b58(nodekey.key_path(n))
            status, _ = signed(cls.port, n, "/register", {"node": n, "kind": "generic", "pubkey": pub})
            assert status == 200, status

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        tabd.DRAIN_SEC, tabd.DRAIN_MAX_BYTES, tabd.HOLD_SEC, tabd.BODY_READ_SEC = cls.saved

    def connect(self, rcvbuf=None):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if rcvbuf:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        s.settimeout(10)
        s.connect(("127.0.0.1", self.port))
        self.addCleanup(s.close)
        return s

    def stored(self, subject):
        con = tabus.connect()
        try:
            return con.execute("SELECT COUNT(*) FROM messages WHERE subject=?", (subject,)).fetchone()[0]
        finally:
            con.close()

    def rows(self):
        con = tabus.connect()
        try:
            return tuple(con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("messages", "deliveries"))
        finally:
            con.close()

    def send_whole(self, subject):
        """One ordinary send on its own connection; returns the status line."""
        body = json.dumps({"from": "mina", "to": ["yuna"], "subject": subject, "body": "b"})
        s = self.connect()
        s.sendall(signed_head("mina", "POST", "/send", body) + body.encode())
        data, _, how, first = until_end(s, time.monotonic(), 10)
        return data.split(b"\r\n", 1)[0]

    def test_stalled_body_is_dropped_after_idle(self):
        body = json.dumps({"from": "mina", "to": ["yuna"], "subject": "stalled", "body": "b"})
        s = self.connect()
        started = time.monotonic()
        s.sendall(signed_head("mina", "POST", "/send", body) + body.encode()[:10])
        data, seconds, how, first = until_end(s, started, limit=5)
        self.assertEqual((data, how), (b"", "closed"))
        self.assertLess(seconds, self.IDLE + 1)
        self.assertEqual(self.stored("stalled"), 0)

    def test_connection_without_a_request_is_closed_after_idle(self):
        s = self.connect()
        data, seconds, how, first = until_end(s, time.monotonic(), limit=5)
        self.assertEqual((data, how), (b"", "closed"))
        self.assertLess(seconds, self.IDLE + 1)

    def test_slow_send_inside_both_limits_is_stored(self):
        # Pauses shorter than the idle limit, and the whole body inside the body limit.
        body = json.dumps({"from": "mina", "to": ["yuna"], "subject": "slow", "body": "x" * 20000}).encode()
        s = self.connect()
        started = time.monotonic()
        s.sendall(signed_head("mina", "POST", "/send", body.decode()))
        step = -(-len(body) // 4)
        for i in range(0, len(body), step):
            time.sleep(self.IDLE * 0.4)
            s.sendall(body[i:i + step])
        data, seconds, how, first = until_end(s, started, limit=10)
        self.assertTrue(data.startswith(b"HTTP/1.0 200"), data[:80])
        self.assertGreater(seconds, self.IDLE)
        self.assertLess(seconds, self.BODY_SEC)
        self.assertEqual(self.stored("slow"), 1)

    def test_body_that_takes_longer_than_the_body_limit_is_refused(self):
        # 🔴 Pieces arrive inside the idle limit, so only the whole-body limit ends this.
        for subject, piece, pause in (("trickle", b"a" * 1024, 0.3), ("one_byte", b"a", 0.5),
                                      ("just_under_idle", b"a", self.IDLE * 0.9)):
            body = json.dumps({"from": "mina", "to": ["yuna"], "subject": subject, "body": "x" * 40000}).encode()
            s = self.connect()
            stop = threading.Event()
            started = time.monotonic()
            s.sendall(signed_head("mina", "POST", "/send", body.decode()))
            rings = len(RINGS)

            def trickle():
                try:
                    while not stop.is_set():
                        s.sendall(piece)
                        time.sleep(pause)
                except OSError:
                    pass

            sender = threading.Thread(target=trickle, daemon=True)
            sender.start()
            data, seconds, how, first = until_end(s, started, limit=15)
            stop.set()
            sender.join(2)
            head, _, answer = data.partition(b"\r\n\r\n")
            self.assertTrue(head.startswith(b"HTTP/1.0 408"), (subject, how, data[:80]))
            self.assertEqual(json.loads(answer), {"error": "request body took too long", "code": "REQUEST_TIMEOUT",
                                                  "message": f"the request body did not arrive within {self.BODY_SEC} seconds",
                                                  "details": {"limit": self.BODY_SEC, "unit": "seconds"}, "retry": "as_is"})
            self.assertGreaterEqual(first, self.BODY_SEC - 0.1, subject)
            # 🔴 Tight on purpose: each read waits the time left rather than a whole idle period,
            #    so the answer does not arrive a pause (or an idle limit) late.
            self.assertLess(first, self.BODY_SEC + 0.3, subject)
            self.assertEqual(self.stored(subject), 0, subject)
            self.assertEqual(len(RINGS), rings, "a refused request rings nothing")

    def test_a_timed_out_body_stores_nothing_and_a_whole_one_stores(self):
        # 🔴 REQUEST_TIMEOUT means the body never reached bus_send. Pinned against a control
        #    send so the pair fails if the code is ever reused after something is stored.
        before = self.rows()
        rings = len(RINGS)
        body = json.dumps({"from": "mina", "to": ["yuna"], "subject": "half", "body": "x" * 40000}).encode()
        s = self.connect()
        started = time.monotonic()
        s.sendall(signed_head("mina", "POST", "/send", body.decode()) + body[:1000])
        stop = threading.Event()

        def trickle():
            try:
                while not stop.is_set():
                    s.sendall(b"a" * 512)
                    time.sleep(0.4)
            except OSError:
                pass

        sender = threading.Thread(target=trickle, daemon=True)
        sender.start()
        data, seconds, how, first = until_end(s, started, limit=15)
        stop.set()
        sender.join(2)
        self.assertTrue(data.startswith(b"HTTP/1.0 408"), (how, data[:80]))
        self.assertEqual(self.rows(), before, "nothing is written for a timed-out body")
        self.assertEqual(len(RINGS), rings)
        self.assertEqual(self.send_whole("control"), b"HTTP/1.0 200 OK")
        self.assertEqual(self.rows(), (before[0] + 1, before[1] + 1), "the control send is stored")

    def test_body_that_ends_before_the_declared_length_is_refused(self):
        # 🔴 The client sends the whole signed JSON but declares more, then closes its write side.
        #    The signature covers what arrived, so only this check refuses it (jiso, jack M3).
        before = self.rows()
        rings = len(RINGS)
        body = json.dumps({"from": "mina", "to": ["yuna"], "subject": "short_body", "body": "x" * 100})
        for extra, sent in ((100, body.encode()), (len(body), b""), (1, body.encode()[:-1])):
            s = self.connect()
            s.sendall(signed_head("mina", "POST", "/send", body, length=len(body.encode()) + extra - (len(body.encode()) - len(sent))))
            s.sendall(sent)
            s.shutdown(socket.SHUT_WR)
            data, seconds, how, first = until_end(s, time.monotonic(), 10)
            head, _, answer = data.partition(b"\r\n\r\n")
            self.assertTrue(head.startswith(b"HTTP/1.0 400"), (extra, how, data[:80]))
            r = json.loads(answer)
            self.assertEqual((r["error"], r["code"], r["retry"]),
                             ("request body is shorter than Content-Length", "REQUEST_INCOMPLETE", "as_is"))
            self.assertEqual(r["details"], {"bytes": len(sent), "declared": len(sent) + extra, "unit": "bytes"})
            self.assertLess(seconds, self.BODY_SEC, "an ended body is refused at once, not after the body limit")
        self.assertEqual(self.rows(), before, "nothing is stored for a short body")
        self.assertEqual(len(RINGS), rings)
        self.assertEqual(self.send_whole("exact_length"), b"HTTP/1.0 200 OK", "the same request with the right length is stored")
        self.assertEqual(self.rows(), (before[0] + 1, before[1] + 1))

    def test_body_limit_is_not_applied_to_a_get_without_a_body(self):
        # /who carries no body: the request is answered well after the body limit would have passed.
        s = self.connect()
        started = time.monotonic()
        s.sendall(signed_head("mina", "GET", "/who", ""))
        data, seconds, how, first = until_end(s, started, limit=10)
        self.assertTrue(data.startswith(b"HTTP/1.0 200"), data[:80])

    def test_get_with_a_slow_body_is_refused_too(self):
        # The signature covers the body on GET as well, so the same limit applies.
        path = "/pull?node=sora&limit=1"
        s = self.connect()
        stop = threading.Event()
        started = time.monotonic()
        s.sendall(signed_head("sora", "GET", path, "", length=200))

        def trickle():
            try:
                while not stop.is_set():
                    s.sendall(b"a")
                    time.sleep(0.5)
            except OSError:
                pass

        sender = threading.Thread(target=trickle, daemon=True)
        sender.start()
        data, seconds, how, first = until_end(s, started, limit=15)
        stop.set()
        sender.join(2)
        self.assertTrue(data.startswith(b"HTTP/1.0 408"), (how, data[:80]))
        self.assertLess(seconds, self.BODY_SEC + 1)

    def test_long_poll_hold_is_not_an_idle_read(self):
        path = "/pull?node=sora&limit=1"
        s = self.connect()
        started = time.monotonic()
        s.sendall(signed_head("sora", "GET", path, ""))
        data, seconds, how, first = until_end(s, started, limit=10)
        self.assertTrue(data.startswith(b"HTTP/1.0 200"), data[:80])
        self.assertGreaterEqual(seconds, self.HOLD - 0.1)

    def test_discard_reads_up_to_its_byte_limit_only(self):
        # Headers only, write side left open: a discarding daemon waits one idle period for the body.
        for length, discards in ((self.DRAIN_MAX, True), (self.DRAIN_MAX + 1, False)):
            s = self.connect()
            started = time.monotonic()
            s.sendall(signed_head("mina", "POST", "/send", "", length=length))
            data, seconds, how, first = until_end(s, started, limit=10)
            self.assertTrue(data.startswith(b"HTTP/1.0 413"), (length, data[:80]))
            if discards:
                self.assertGreaterEqual(first, self.IDLE - 0.1, length)
            else:
                self.assertLess(first, self.IDLE / 2, length)

    def test_discard_stops_at_its_time_limit(self):
        # 1 KiB every 0.3 s: each read gets data before the idle limit, and one read of 64 KiB
        # would take 19 s. Every 0.01 s: the whole 1 MiB takes about 10 s. Only the time limit ends either.
        for pause in (0.3, 0.01):
            s = self.connect()
            stop = threading.Event()
            started = time.monotonic()
            s.sendall(signed_head("mina", "POST", "/send", "", length=self.DRAIN_MAX))

            def trickle():
                try:
                    while not stop.is_set():
                        s.sendall(b"a" * 1024)
                        time.sleep(pause)
                except OSError:
                    pass

            sender = threading.Thread(target=trickle, daemon=True)
            sender.start()
            data, seconds, how, first = until_end(s, started, limit=15)
            stop.set()
            sender.join(2)
            self.assertGreaterEqual(seconds, self.DRAIN_SEC - 0.1, (pause, how, data[:80]))
            self.assertLess(seconds, self.DRAIN_SEC + 1, (pause, how, data[:80]))

    def test_discard_does_not_wait_a_full_idle_past_its_time_limit(self):
        # Data until just before the time limit, then silence: the last read waits only the time left.
        s = self.connect()
        started = time.monotonic()
        s.sendall(signed_head("mina", "POST", "/send", "", length=self.DRAIN_MAX))
        while time.monotonic() - started < self.DRAIN_SEC - 0.2:
            s.sendall(b"a" * 1024)
            time.sleep(0.1)
        data, seconds, how, first = until_end(s, started, limit=15)
        self.assertTrue(data.startswith(b"HTTP/1.0 413"), (how, data[:80]))
        self.assertLess(seconds, self.DRAIN_SEC + self.IDLE / 2, how)

    def test_response_write_has_no_time_limit(self):
        # \ud83d\udd34 A socket timeout bounds a whole sendall, not the pause between sends. A reader that
        #    takes longer than idle to drain a large response must still get all of it.
        con = tabus.connect()
        try:
            for i in range(30):
                mid, info = tabus.bus_send(con, "mina", ["yuna"], f"big{i}", chr(1) * 65536)
                assert mid, info
        finally:
            con.close()
        path = "/pull?node=yuna&limit=50"
        s = self.connect(rcvbuf=4096)
        s.sendall(signed_head("yuna", "GET", path, ""))
        time.sleep(self.IDLE * 2.5)
        data, seconds, how, first = until_end(s, time.monotonic(), limit=30)
        head, _, body = data.partition(b"\r\n\r\n")
        self.assertTrue(head.startswith(b"HTTP/1.0 200"), head[:80])
        length = int([h.split(b":")[1] for h in head.split(b"\r\n") if h.lower().startswith(b"content-length:")][0])
        self.assertGreater(length, 10 * 1024 * 1024)
        self.assertEqual(len(body), length, how)
        self.assertEqual(len([m for m in json.loads(body)["messages"] if m["subject"].startswith("big")]), 30)


if __name__ == "__main__":
    unittest.main()
