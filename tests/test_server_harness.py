#!/usr/bin/env python3
"""The harness that gives a test its own daemon.

Pins:
- The port is never one the bus uses, even when it is free at that moment.
- A port another process holds makes `start` raise, and the caller sends no request.
  This is the case that made the acceptance test talk to the live bus.
- 🔴 A listener that takes the port after the check, while the child is still starting,
  does not count as ready. Answering plus a living child was the readiness rule until hu
  and jack reproduced this case; the rule is now the child's own report of its bind.
- The daemon runs with every path inside the test's directory, whatever the caller's
  environment says.
- A daemon that cannot come up makes `start` raise, with what the daemon printed.
- `stop` gives the port back, so the next run does not meet a daemon it did not start.

Every case here uses a listener this file creates and a daemon this file spawns. Nothing
in it reaches a port the bus uses.
"""

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import server_harness as harness  # noqa: E402


def ready_line(port, bind="127.0.0.1"):
    """A line shaped the way the harness expects one.

    🔴 Written here rather than taken from the daemon: a test that borrows the product's
    value proves only that the two sides use the same string, and borrowing it is what
    made the parent and the child read different installations (woo, jack). What the two
    sides agree on is checked by `test_the_daemon_prints_what_this_harness_looks_for`,
    which starts a daemon and reads the line it really printed.
    """
    return (f"[tabd] http://{bind}:{port} listening. "
            "Every request is authenticated by node-key signature.\n")


class Ports(unittest.TestCase):
    def test_a_free_port_is_never_one_the_bus_uses(self):
        ports = {harness.free_port() for _ in range(25)}
        self.assertFalse(ports & set(harness.FORBIDDEN_PORTS))
        for port in ports:
            self.assertTrue(1024 < port < 65536, port)

    def test_a_bus_port_is_refused_before_anything_starts(self):
        with tempfile.TemporaryDirectory(prefix="harness_") as tmp:
            for port in harness.FORBIDDEN_PORTS:
                with self.assertRaises(harness.ServerStartFailed) as caught:
                    harness.start(ROOT, tmp, os.path.join(tmp, "t.db"), port=port)
                self.assertIn(str(port), str(caught.exception))


    def test_the_bus_this_environment_points_at_is_refused_too(self):
        # 🔴 The constants are a list kept by hand. A bus running on a third port is only
        #    guarded because the environment says where it is (jack).
        with tempfile.TemporaryDirectory(prefix="harness_") as tmp:
            elsewhere = harness.free_port()
            previous = os.environ.get("TABC_BUS_URL")
            os.environ["TABC_BUS_URL"] = f"http://127.0.0.1:{elsewhere}"
            try:
                self.assertIn(elsewhere, harness.forbidden_ports())
                self.assertNotIn(elsewhere, {harness.free_port() for _ in range(25)})
                with self.assertRaises(harness.ServerStartFailed) as caught:
                    harness.start(ROOT, tmp, os.path.join(tmp, "t.db"), port=elsewhere)
                self.assertIn("belongs to the bus", str(caught.exception))
                os.environ["TABC_BUS_URL"] = "not a url"
                self.assertEqual(harness.forbidden_ports(), set(harness.FORBIDDEN_PORTS))
            finally:
                if previous is None:
                    os.environ.pop("TABC_BUS_URL", None)
                else:
                    os.environ["TABC_BUS_URL"] = previous


class StartFailures(unittest.TestCase):
    """🔴 Both cases end in a raise. A caller that waits a fixed time instead would send
    its next request to whatever holds the port."""

    def test_a_port_another_process_holds(self):
        with tempfile.TemporaryDirectory(prefix="harness_") as tmp:
            held = socket.socket()
            held.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            held.bind(("127.0.0.1", 0))
            held.listen(64)
            port = held.getsockname()[1]
            self.addCleanup(held.close)
            # The port answers, so a readiness check that only connects would pass here.
            self.assertTrue(harness.answers(port))
            dbpath = os.path.join(tmp, "t.db")
            with self.assertRaises(harness.ServerStartFailed) as caught:
                harness.start(ROOT, tmp, dbpath, port=port, wait=8.0)
            # 🔴 The daemon opens its ledger before it binds, so the file exists; what
            #    matters is that the bind failed and the caller was told, rather than
            #    being handed a port another process answers on.
            self.assertIn("already held by another process", str(caught.exception))
            self.assertIn(str(port), str(caught.exception))
            self.assertFalse(os.path.exists(dbpath), "nothing was started")

    def test_a_daemon_that_cannot_come_up(self):
        with tempfile.TemporaryDirectory(prefix="harness_") as tmp:
            # 🔴 A ledger path inside a directory that does not exist: the daemon fails
            #    while opening it, and the failure has to surface as a raise.
            dbpath = os.path.join(tmp, "absent", "t.db")
            with self.assertRaises(harness.ServerStartFailed) as caught:
                harness.start(ROOT, tmp, dbpath, wait=8.0)
            self.assertIn("tabd exited", str(caught.exception))

    def test_a_port_held_on_the_wildcard_address(self):
        # 🔴 The holder binds 0.0.0.0 and the daemon binds 127.0.0.1. A probe that asks
        #    only the daemon's address binds this port successfully and reports it free.
        with tempfile.TemporaryDirectory(prefix="harness_") as tmp:
            held = socket.socket()
            held.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            held.bind(("0.0.0.0", 0))
            held.listen(64)
            port = held.getsockname()[1]
            self.addCleanup(held.close)
            self.assertTrue(harness.held_by_another(port))
            dbpath = os.path.join(tmp, "t.db")
            with self.assertRaises(harness.ServerStartFailed) as caught:
                harness.start(ROOT, tmp, dbpath, port=port, wait=8.0)
            self.assertIn("already held by another process", str(caught.exception))
            self.assertFalse(os.path.exists(dbpath), "nothing was started")


class TimeWaitIsNotAHolder(unittest.TestCase):
    """🔴 A port that answers nothing and belongs to nobody, which a plain bind refuses.

    Closing a connection from the listening side leaves a socket in TIME_WAIT on that
    port. No process holds the port then, and tabd binds it, because `http.server` sets
    allow_reuse_address. A probe that binds without that option refuses anyway, and the
    harness stops a test that could have run; that happened on a port passed explicitly,
    with `lsof` showing no process on it.
    """

    def time_wait_port(self):
        """A port with a TIME_WAIT left on it and nothing holding it.

        🔴 The state is checked here, before the port is handed out, rather than only in
        a test of its own. Each call makes a different port, so a test that proves the
        state for the port it received says nothing about the ports the other calls get;
        on a machine where TIME_WAIT sometimes fails to appear, those tests would run
        against a merely free port and pass against the defective probe.
        """
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        client = socket.create_connection(("127.0.0.1", port), 2.0)
        accepted, _ = listener.accept()
        accepted.close()  # closed from this side, so TIME_WAIT lands on this port
        listener.close()
        client.close()
        probe = socket.socket()
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return port  # refused, which is the state these tests are about
        finally:
            probe.close()
        raise AssertionError(
            f"port {port} accepted a plain bind, so no TIME_WAIT is on it; the tests "
            "below would run against a merely free port"
        )

    def test_the_state_these_tests_need_is_really_there(self):
        # 🔴 The helper enforces this for every caller; this test states the condition in
        #    one place so a reader sees what the other two tests stand on.
        port = self.time_wait_port()
        plain = socket.socket()
        self.addCleanup(plain.close)
        with self.assertRaises(OSError):
            plain.bind(("127.0.0.1", port))

    def test_a_port_in_time_wait_is_not_held(self):
        port = self.time_wait_port()
        self.assertFalse(harness.answers(port), "nothing accepts a connection there")
        self.assertFalse(harness.held_by_another(port))

    def test_a_daemon_starts_on_a_port_in_time_wait(self):
        port = self.time_wait_port()
        with tempfile.TemporaryDirectory(prefix="harness_") as tmp:
            proc, bound, _base, _log = harness.start(
                ROOT, tmp, os.path.join(tmp, "t.db"), port=port, wait=20.0
            )
            self.addCleanup(harness.stop, proc)
            self.assertEqual(bound, port)


class ReadinessCounterExamples(unittest.TestCase):
    """🔴 The window between checking the port and the child binding it.

    Both cases use a listener this file opens and a child that is not tabd, so the
    readiness rule is exercised on its own, with no daemon and no request.
    """

    def slow_child(self):
        # 🔴 Alive, and nothing to do with the port: what a daemon looks like between its
        #    spawn and its bind.
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
        self.addCleanup(harness.stop, proc)
        return proc

    def test_another_listener_on_the_port_is_not_readiness(self):
        with tempfile.TemporaryDirectory(prefix="harness_") as tmp:
            held = socket.socket()
            held.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            held.bind(("127.0.0.1", 0))
            held.listen(64)
            port = held.getsockname()[1]
            self.addCleanup(held.close)
            proc = self.slow_child()
            log = os.path.join(tmp, "tabd.log")
            open(log, "w").close()
            # The three signals the old rule used are all true here.
            self.assertTrue(harness.answers(port))
            self.assertIsNone(proc.poll())
            self.assertTrue(os.path.exists(log))
            with self.assertRaises(harness.ServerStartFailed) as caught:
                harness.wait_until_ready(proc, port, log, time.time() + 1.0)
            self.assertIn("printed nothing", str(caught.exception))

    def test_a_report_for_a_different_port_is_not_readiness(self):
        with tempfile.TemporaryDirectory(prefix="harness_") as tmp:
            proc = self.slow_child()
            log = os.path.join(tmp, "tabd.log")
            with open(log, "w", encoding="utf-8") as fh:
                fh.write(ready_line(9))
            with self.assertRaises(harness.ServerStartFailed) as caught:
                harness.wait_until_ready(proc, 10, log, time.time() + 1.0)
            self.assertIn("reported port 9", str(caught.exception))

    def test_the_childs_own_report_is_what_passes(self):
        with tempfile.TemporaryDirectory(prefix="harness_") as tmp:
            proc = self.slow_child()
            log = os.path.join(tmp, "tabd.log")
            with open(log, "w", encoding="utf-8") as fh:
                fh.write(ready_line(4242))
            self.assertTrue(harness.wait_until_ready(proc, 4242, log, time.time() + 1.0))

    def test_a_child_that_exits_is_reported_with_its_output(self):
        with tempfile.TemporaryDirectory(prefix="harness_") as tmp:
            log = os.path.join(tmp, "tabd.log")
            with open(log, "w", encoding="utf-8") as fh:
                fh.write("Traceback: OSError address already in use\n")
            proc = subprocess.Popen([sys.executable, "-c", "raise SystemExit(3)"])
            proc.wait(timeout=10)
            with self.assertRaises(harness.ServerStartFailed) as caught:
                harness.wait_until_ready(proc, 4242, log, time.time() + 1.0)
            self.assertIn("exited with 3", str(caught.exception))
            self.assertIn("address already in use", str(caught.exception))


    def test_the_race_end_to_end_with_a_real_daemon(self):
        """🔴 The reproduction hu and jack sent, run against tabd itself.

        The check that the port is free is forced to pass while a listener holds it, which
        is what a lost race looks like from inside `start`. The daemon cannot bind, so the
        readiness report never comes and `start` raises. It returned success here until the
        rule changed.
        """
        with tempfile.TemporaryDirectory(prefix="harness_") as tmp:
            held = socket.socket()
            held.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            held.bind(("127.0.0.1", 0))
            held.listen(64)
            port = held.getsockname()[1]
            self.addCleanup(held.close)
            original = harness.held_by_another
            harness.held_by_another = lambda _port: False
            self.addCleanup(setattr, harness, "held_by_another", original)
            with self.assertRaises(harness.ServerStartFailed) as caught:
                harness.start(ROOT, tmp, os.path.join(tmp, "t.db"), port=port, wait=12.0)
            self.assertIn("exited", str(caught.exception))
            self.assertTrue(harness.answers(port), "the listener still holds the port")


    def test_an_unexpected_exception_still_ends_the_child(self):
        """🔴 The readiness step can fail in ways this module does not define — an import
        that is not there, a Ctrl-C. The child is already running by then, and the caller
        has no handle to it yet, so `start` has to end it before the exception leaves
        (hu). Measured: a missing dependency did exactly this."""
        for blow_up in (ImportError("no module"), KeyboardInterrupt(), RuntimeError("x")):
            with tempfile.TemporaryDirectory(prefix="harness_") as tmp:
                spawned = []

                def explode(proc, *rest, _raise=blow_up):
                    # 🔴 The child is captured here, because the caller never receives it
                    #    when start raises. Asking the process is the only answer that does
                    #    not depend on how far along its own startup it was: a port check
                    #    would pass simply because the daemon had not bound yet.
                    spawned.append(proc)
                    raise _raise

                original = harness.wait_until_ready
                harness.wait_until_ready = explode
                try:
                    with self.assertRaises(type(blow_up)):
                        harness.start(ROOT, tmp, os.path.join(tmp, "t.db"))
                finally:
                    harness.wait_until_ready = original
                self.assertEqual(len(spawned), 1, f"no child was started for {blow_up!r}")
                self.addCleanup(harness.stop, spawned[0])
                self.assertIsNotNone(spawned[0].poll(),
                                     f"the child outlived {blow_up!r}")


class TheAgreementWithTheDaemon(unittest.TestCase):
    def test_the_daemon_prints_what_this_harness_looks_for(self):
        """🔴 The one place the two wordings are compared, and they are compared by
        starting a daemon and reading what it actually printed — not by sharing a string.
        A failure here means the wording moved, not that the daemon is broken."""
        with tempfile.TemporaryDirectory(prefix="harness_") as tmp:
            # 🔴 The daemon is started directly, not through `start`: `start` waits for
            #    this very line, so a wording change would fail there first and read as
            #    "the daemon did not come up". Here the only question asked is what it
            #    printed (jack).
            dbpath = os.path.join(tmp, "t.db")
            port = harness.free_port()
            env = harness.isolated_env(tmp, dbpath, f"http://127.0.0.1:{port}")
            env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
            log = os.path.join(tmp, "direct.log")
            with open(log, "wb") as sink:
                proc = subprocess.Popen(
                    [sys.executable, "-m", "tabus.daemon", "--port", str(port)],
                    cwd=tmp, env=env, stdout=sink, stderr=subprocess.STDOUT)
            self.addCleanup(harness.stop, proc)
            # 🔴 Waiting for the port to answer is not enough: the socket is bound
            #    before the line is printed, so reading then can catch an empty log and
            #    report a wording change that did not happen. This waits for the line
            #    itself, and gives up after a few seconds so a real change is reported
            #    with whatever the daemon did print.
            deadline = time.time() + 12
            printed = ""
            while time.time() < deadline:
                if proc.poll() is not None:
                    self.fail(f"the daemon exited: {open(log, encoding='utf-8').read()[-400:]}")
                printed = open(log, encoding="utf-8").read()
                if harness.READY.search(printed):
                    break
                time.sleep(0.05)
            printed = printed.strip()
            harness.stop(proc)
            found = harness.READY.search(printed)
            self.assertIsNotNone(
                found,
                "the daemon's announcement no longer matches what server_harness.READY "
                f"looks for — the wording changed in one of the two places. The daemon "
                f"printed: {printed[-400:]}",
            )
            self.assertEqual(
                int(found.group(1)), port,
                f"the harness read port {found.group(1)} out of the daemon's line, but it "
                f"was started on {port}. Printed: {printed[-400:]}",
            )


class Isolation(unittest.TestCase):
    def test_importing_the_harness_does_not_import_the_package(self):
        # 🔴 The invariant that lets a test set its own TABC_HOME after importing this
        #    helper. `tabus.paths` reads that variable once, at its import; a helper that
        #    pulled the package in first would freeze the caller's home, and the keys a
        #    test signs with would come from a different directory than the package reads.
        probe = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, sys.argv[1]); import server_harness; "
             "print(any(m == 'tabus' or m.startswith('tabus.') for m in sys.modules))",
             os.path.dirname(os.path.abspath(__file__))],
            capture_output=True, text=True,
        )
        self.assertEqual(probe.stdout.strip(), "False", probe.stderr)


    def test_every_path_lands_inside_the_test_directory(self):
        with tempfile.TemporaryDirectory(prefix="harness_") as tmp:
            env = harness.isolated_env(tmp, os.path.join(tmp, "t.db"), "http://127.0.0.1:1")
            for name in ("TABC_HOME", "TABC_DB", "TABC_DOORBELL_SPOOL"):
                self.assertTrue(os.path.realpath(env[name]).startswith(os.path.realpath(tmp)),
                                f"{name}={env[name]}")
            self.assertTrue(os.path.isdir(env["TABC_HOME"]))

    def test_a_path_outside_the_test_directory_is_refused(self):
        # 🔴 A harness that claims isolation has to check the isolation (2026-09-15).
        #    Every way out is refused, not only the one that names the operating home.
        with tempfile.TemporaryDirectory(prefix="harness_") as tmp:
            outside = tempfile.mkdtemp(prefix="harness_other_")
            self.addCleanup(shutil.rmtree, outside, True)
            home = os.path.expanduser("~/.tabc")
            for override in ({"TABC_HOME": home},
                             {"TABC_DB": os.path.join(home, "tabus.db")},
                             {"TABC_DOORBELL_SPOOL": os.path.join(home, "doorbell")},
                             {"TABC_HOME": outside},
                             {"TABC_DB": os.path.join(outside, "x.db")},
                             {"TABC_DOORBELL_SPOOL": os.path.join(outside, "ring")}):
                with self.assertRaises(harness.ServerStartFailed) as caught:
                    harness.isolated_env(tmp, os.path.join(tmp, "t.db"),
                                         "http://127.0.0.1:1", override)
                self.assertIn("outside the directory this test owns", str(caught.exception))
            # The ledger argument itself is a path like any other.
            with self.assertRaises(harness.ServerStartFailed):
                harness.isolated_env(tmp, os.path.join(outside, "external.db"),
                                     "http://127.0.0.1:1")

    def test_a_sibling_with_the_same_prefix_is_outside(self):
        # 🔴 The case a string comparison gets wrong: /…/run is a prefix of /…/runaway.
        with tempfile.TemporaryDirectory(prefix="harness_") as parent:
            root = os.path.join(parent, "run")
            sibling = os.path.join(parent, "runaway")
            os.makedirs(root)
            os.makedirs(sibling)
            self.assertTrue(harness.inside(root, os.path.join(root, "a", "b")))
            self.assertFalse(harness.inside(root, os.path.join(sibling, "a")))
            with self.assertRaises(harness.ServerStartFailed):
                harness.isolated_env(root, os.path.join(root, "t.db"),
                                     "http://127.0.0.1:1",
                                     {"TABC_HOME": os.path.join(sibling, "home")})

    def test_a_symlink_that_leaves_the_directory_is_outside(self):
        # 🔴 A path inside the directory that resolves outside it. realpath answers this;
        #    comparing the written path does not.
        with tempfile.TemporaryDirectory(prefix="harness_") as tmp:
            outside = tempfile.mkdtemp(prefix="harness_other_")
            self.addCleanup(shutil.rmtree, outside, True)
            link = os.path.join(tmp, "home")
            os.symlink(outside, link)
            self.assertFalse(harness.inside(tmp, link))
            with self.assertRaises(harness.ServerStartFailed) as caught:
                harness.isolated_env(tmp, os.path.join(tmp, "t.db"), "http://127.0.0.1:1")
            self.assertIn("TABC_HOME", str(caught.exception))


class Lifetime(unittest.TestCase):
    def test_start_then_stop_returns_the_port(self):
        with tempfile.TemporaryDirectory(prefix="harness_") as tmp:
            dbpath = os.path.join(tmp, "t.db")
            proc, port, base, log = harness.start(ROOT, tmp, dbpath)
            self.addCleanup(harness.stop, proc)
            self.assertIsNone(proc.poll(), "the daemon this test started is running")
            self.assertTrue(harness.answers(port))
            self.assertTrue(os.path.exists(dbpath), "it created its own ledger")
            self.assertEqual(base, f"http://127.0.0.1:{port}")
            self.assertTrue(os.path.exists(log))
            harness.stop(proc)
            self.assertIsNotNone(proc.poll())
            self.assertFalse(harness.answers(port), "the port is free again")
            harness.stop(proc)  # calling it twice is safe


if __name__ == "__main__":
    unittest.main()
