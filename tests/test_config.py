"""config.json: settings a person changes without editing code.

Order for each setting: environment variable, then config.json in TABC_HOME, then
the built-in default. A config.json that exists but is wrong stops the caller
with the file and key named; it is never ignored.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tabus import doorbell_poll, paths  # noqa: E402

ENV = ("TABC_HOME", "TABC_BUS_URL", "DOORBELL_POLL_SEC")


class Config(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.pop(k, None) for k in ENV}
        self.home = tempfile.mkdtemp(prefix="tabc-config-")
        os.environ["TABC_HOME"] = self.home

    def tearDown(self):
        for k in ENV:
            os.environ.pop(k, None)
            if self.saved[k] is not None:
                os.environ[k] = self.saved[k]
        for fn in os.listdir(self.home):
            os.remove(os.path.join(self.home, fn))
        os.rmdir(self.home)

    def write(self, data):
        with open(os.path.join(self.home, "config.json"), "w", encoding="utf-8") as f:
            f.write(data if isinstance(data, str) else json.dumps(data))

    def test_no_file_gives_defaults(self):
        self.assertEqual(paths.bus_port(), paths.DEFAULT_BUS_PORT)
        self.assertEqual(paths.bus_url(), paths.DEFAULT_BUS_URL)
        self.assertEqual(doorbell_poll.poll_interval(), paths.DEFAULT_DOORBELL_POLL_SEC)

    def test_reading_settings_does_not_create_home(self):
        os.rmdir(self.home)
        try:
            self.assertEqual(paths.bus_url(), paths.DEFAULT_BUS_URL)
            self.assertFalse(os.path.exists(self.home))
        finally:
            os.makedirs(self.home)

    def test_port_moves_tabd_and_local_clients_together(self):
        self.write({"port": 9001})
        self.assertEqual(paths.bus_port(), 9001)
        self.assertEqual(paths.bus_url(), "http://127.0.0.1:9001")

    def test_bus_url_and_interval_from_file(self):
        self.write({"bus_url": "http://10.0.0.5:8765", "doorbell_poll_sec": 0.5})
        self.assertEqual(paths.bus_url(), "http://10.0.0.5:8765")
        self.assertEqual(doorbell_poll.poll_interval(), 0.5)

    def test_environment_wins_over_file(self):
        self.write({"bus_url": "http://10.0.0.5:8765", "doorbell_poll_sec": 0.5})
        os.environ["TABC_BUS_URL"] = "http://127.0.0.1:7000"
        os.environ["DOORBELL_POLL_SEC"] = "3"
        self.assertEqual(paths.bus_url(), "http://127.0.0.1:7000")
        self.assertEqual(doorbell_poll.poll_interval(), 3.0)

    def test_broken_file_is_refused_with_key_named(self):
        cases = [
            ("{not json", "not JSON"),
            ("[1]", "JSON object"),
            ({"prot": 9001}, "unknown key 'prot'"),
            ({"port": "9001"}, "port must be"),
            ({"port": 0}, "port must be"),
            ({"port": True}, "port must be"),
            ({"bus_url": "127.0.0.1:8765"}, "bus_url must be"),
            ({"doorbell_poll_sec": 0}, "doorbell_poll_sec must be"),
            ({"doorbell_poll_sec": "2"}, "doorbell_poll_sec must be"),
            # explicit null is refused, not read as "missing"
            ({"port": None}, "port is null"),
            ({"bus_url": None}, "bus_url is null"),
            ({"doorbell_poll_sec": None}, "doorbell_poll_sec is null"),
            # not finite, or too large for time.sleep
            ('{"doorbell_poll_sec": Infinity}', "not JSON"),
            ('{"doorbell_poll_sec": -Infinity}', "not JSON"),
            ('{"doorbell_poll_sec": NaN}', "not JSON"),
            ('{"doorbell_poll_sec": 1e309}', "doorbell_poll_sec must be"),
            ({"doorbell_poll_sec": 1e20}, "doorbell_poll_sec must be"),
            ({"doorbell_poll_sec": -1}, "doorbell_poll_sec must be"),
            ({"doorbell_poll_sec": True}, "doorbell_poll_sec must be"),
            # a JSON integer too large for float: range is tested before conversion
            ('{"doorbell_poll_sec": ' + "9" * 400 + "}", "doorbell_poll_sec must be"),
            ('{"doorbell_poll_sec": -' + "9" * 400 + "}", "doorbell_poll_sec must be"),
            ('{"port": ' + "9" * 400 + "}", "port must be"),
            # a URL a client cannot use
            ({"bus_url": "http://"}, "bus_url must be"),
            ({"bus_url": "http://:8765"}, "bus_url must be"),
            ({"bus_url": "ftp://127.0.0.1:8765"}, "bus_url must be"),
            ({"bus_url": "http://127.0.0.1:99999"}, "bus_url is not a usable URL"),
            ({"bus_url": "http://127.0.0.1:8765/x"}, "bus_url must be"),
            ({"bus_url": "http://u:p@127.0.0.1:8765"}, "bus_url must be"),
            ({"bus_url": 8765}, "bus_url must be a string"),
            # values urlsplit accepts but a request cannot use
            ({"bus_url": "http://127.0.0.1:8765\n"}, "spaces or control"),
            ({"bus_url": "http://127.0.0.1:8765\t"}, "spaces or control"),
            ({"bus_url": "http://127.0.0.1 :8765"}, "spaces or control"),
            ({"bus_url": "http://127.0.0.1:8765\u007f"}, "spaces or control"),
            # a key twice: json.loads would keep the last silently
            ('{"port": 80, "port": 9000}', "appears twice"),
        ]
        for data, want in cases:
            self.write(data)
            with self.assertRaises(paths.ConfigError, msg=repr(data)) as cm:
                paths.bus_url()
            self.assertIn(want, str(cm.exception), repr(data))
            self.assertIn("config.json", str(cm.exception))

    def test_file_that_is_not_utf8_is_refused_with_file_named(self):
        with open(os.path.join(self.home, "config.json"), "wb") as f:
            f.write(b'{"bus_url": "http://\xff"}')
        with self.assertRaises(paths.ConfigError) as cm:
            paths.bus_url()
        self.assertIn("not UTF-8", str(cm.exception))
        self.assertIn("config.json", str(cm.exception))
        env = dict(os.environ, PYTHONPATH=ROOT)
        h = subprocess.run([sys.executable, "-m", "tabus.cli", "--help"], env=env, capture_output=True, text=True)
        self.assertEqual(h.returncode, 0, h.stderr)

    def test_accepted_values_reach_the_getters_checked(self):
        self.write({"port": 1, "bus_url": "https://example.test/", "doorbell_poll_sec": 86400})
        self.assertEqual(paths.bus_port(), 1)
        self.assertEqual(paths.bus_url(), "https://example.test")
        self.assertEqual(paths.doorbell_poll_sec(), 86400.0)

    def test_environment_interval_is_checked_too(self):
        for bad in ("inf", "nan", "0", "-1", "1e20", "two"):
            os.environ["DOORBELL_POLL_SEC"] = bad
            with self.assertRaises(paths.ConfigError, msg=bad):
                paths.doorbell_poll_sec()

    def test_trailing_slash_is_dropped_so_paths_join(self):
        self.write({"bus_url": "http://127.0.0.1:9001/"})
        self.assertEqual(paths.bus_url(), "http://127.0.0.1:9001")
        os.environ["TABC_BUS_URL"] = "http://127.0.0.1:7000/"
        self.assertEqual(paths.bus_url(), "http://127.0.0.1:7000")
        self.assertEqual(paths.bus_url() + "/who", "http://127.0.0.1:7000/who")

    def test_environment_does_not_hide_a_broken_file(self):
        # README: a broken config.json stops the caller even when an environment
        # variable would have replaced the broken value.
        self.write({"bus_url": "http://", "doorbell_poll_sec": None})
        os.environ["TABC_BUS_URL"] = "http://127.0.0.1:8765"
        os.environ["DOORBELL_POLL_SEC"] = "2"
        with self.assertRaises(paths.ConfigError):
            paths.bus_url()
        with self.assertRaises(paths.ConfigError):
            paths.doorbell_poll_sec()
        with self.assertRaises(paths.ConfigError):
            paths.bus_port()

    def test_doorbell_stops_with_one_line_on_broken_file(self):
        self.write({"doorbell_poll_sec": None})
        env = dict(os.environ, PYTHONPATH=ROOT)
        code = "from tabus import doorbell_poll; doorbell_poll.poll_interval()"
        r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("doorbell: config error", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_cli_help_works_and_bus_command_stops_on_broken_file(self):
        self.write({"prot": 1})
        env = dict(os.environ, PYTHONPATH=ROOT)
        h = subprocess.run([sys.executable, "-m", "tabus.cli", "--help"], env=env, capture_output=True, text=True)
        self.assertEqual(h.returncode, 0, h.stderr)
        r = subprocess.run([sys.executable, "-m", "tabus.cli", "who", "--node", "x"], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("config error", r.stderr)
        self.assertIn("unknown key 'prot'", r.stderr)

    def test_tabd_port_default_from_file_and_refusal(self):
        env = dict(os.environ, PYTHONPATH=ROOT)
        self.write({"prot": 1})
        r = subprocess.run([sys.executable, "-m", "tabus.daemon", "--help"], env=env, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("config error", r.stderr)


if __name__ == "__main__":
    unittest.main()
