"""Starting a tabd that the test owns.

🔴 A test that talks to a fixed port can reach a daemon it did not start. The bus runs on
8765 and 8766 on a machine where the team uses it, so a test aimed at one of those ports
sends signed requests to the live ledger. Every helper here refuses those ports, takes one
the operating system reports free, and proves the daemon answering is the one this test
spawned — before the test sends a single request.

What counts as proof matters, and two weaker answers were tried first.

- Connecting to the port proves nothing: any listener accepts a connection, which is how
  a fixed-port test reached the bus in the first place.
- The spawned process still running proves nothing either. `poll()` says the child has not
  exited yet, not that its bind succeeded. Between the moment this module checks the port
  and the moment the child binds, another process can take it; the child then binds
  nothing, dies a little later, and a readiness check made of "the port answers and the
  child is alive" returns success in between. That window was reproduced (hu, jack).

So readiness is the child saying, in the output file opened for it here, that it bound the
port: tabd prints its address after the socket is taken. Nothing else writes to that file.
"""

import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.parse

# 🔴 The two ports the bus itself uses. A test never takes one, even if it is free at this
#    moment: the daemon may be restarting, and the test would then hold the port the bus
#    is about to take.
FORBIDDEN_PORTS = (8765, 8766)


# 🔴 Written here, not imported from the daemon. Sharing the constant was tried and it
#    made two defects: importing the package froze TABC_HOME at that moment, and the
#    import took whichever tabus the PARENT's sys.path found while the child runs the one
#    on its PYTHONPATH — two processes, two copies, silently different wording (woo, jack).
#    What the two places agree on is checked by starting a daemon and comparing the line
#    it actually prints: `test_server_harness.py` does that, and a mismatch there says the
#    wording changed rather than that the daemon failed.
READY = re.compile(r"\[tabd\] http://[^:\s]+:(\d+) listening\. "
                   r"Every request is authenticated by node-key signature\.")


class ServerStartFailed(RuntimeError):
    """The daemon did not come up on a port this test owns. No request was sent."""


def forbidden_ports():
    """The two constants, plus the port of the bus this environment points at.

    🔴 A list kept by hand only holds what somebody remembered to add. Running the bus on
    a third port would leave that port unguarded, so the environment is asked as well:
    TABC_BUS_URL is where a client in this process would send a request (jack).
    """
    ports = set(FORBIDDEN_PORTS)
    url = os.environ.get("TABC_BUS_URL")
    if url:
        try:
            port = urllib.parse.urlsplit(url).port
        except ValueError:  # a malformed URL says nothing about a port
            port = None
        if port:
            ports.add(port)
    return ports


def free_port(attempts=20):
    """A port the operating system reports free, never one the bus uses."""
    for _ in range(attempts):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        if port not in forbidden_ports():
            return port
    raise ServerStartFailed("no free port outside the ports the bus uses")


# 🔴 Both addresses are asked, and one of them alone gives the wrong answer. With
#    SO_REUSEADDR set, a bind on 127.0.0.1 succeeds while another process listens on
#    0.0.0.0, and a bind on 0.0.0.0 succeeds while another listens on 127.0.0.1 (both
#    measured). A probe on the daemon's own address alone would therefore call a port free
#    while a process holds it, which is the direction this module exists to guard.
PROBE_ADDRESSES = ("127.0.0.1", "0.0.0.0")


def held_by_another(port):
    """True when a process holds this port. A socket in TIME_WAIT is not a holder.

    🔴 Asked by binding it, not by connecting to it. A connection succeeds against any
    listener, which is what made a fixed-port test talk to the bus.

    🔴 Asked with the option the daemon itself binds with. `http.server` sets
    allow_reuse_address, so tabd binds with SO_REUSEADDR; a probe without it refuses in a
    case where tabd would have bound. An earlier connection to this port leaves a socket
    in TIME_WAIT, no process holds the port and nothing answers on it, and a plain bind
    still fails — a test that passed a port explicitly failed that way with the port free.
    Measured here: TIME_WAIT only, plain bind refuses and a
    SO_REUSEADDR bind succeeds; a live listener on the same address, both refuse.

    🔴 Readiness is not decided here. A free port now says nothing about who holds it a
    moment later — repeated checks produced different results, and the verdict belongs to
    `wait_until_ready`, which waits for the child to say it bound.
    """
    return any(_refuses_bind(address, port) for address in PROBE_ADDRESSES)


def _refuses_bind(address, port):
    probe = socket.socket()
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind((address, port))
        return False
    except OSError:
        return True
    finally:
        probe.close()


def answers(port, timeout=0.25):
    """True when something accepts a connection on this port.

    🔴 Readiness is not decided here either, and this one is the tempting mistake: the
    listener answering may be anyone's. Use it to see that a port is in use, never to
    conclude that the daemon this test started is up.
    """
    try:
        with socket.create_connection(("127.0.0.1", port), timeout):
            return True
    except OSError:
        return False


def inside(root, path):
    """Whether this path lies within root, after symlinks are resolved.

    🔴 `startswith` is not this test: `/tmp/run` is a prefix of `/tmp/runaway`, and a
    symlink inside the directory can point anywhere. `realpath` answers the symlink, and
    `commonpath` compares whole path components rather than characters (hu).
    """
    root = os.path.realpath(root)
    target = os.path.realpath(path)
    try:
        return os.path.commonpath([root, target]) == root
    except ValueError:  # different drives, or one path relative and one absolute
        return False


def isolated_env(tmpdir, dbpath, base, env_extra=None):
    """The environment a test daemon runs in, with every path inside tmpdir.

    🔴 Set here rather than left to the caller. A harness that claims isolation and then
    inherits TABC_HOME from whoever ran the tests is isolated only by luck, and a run under
    the operating home would write its keys and its doorbell spool.
    """
    env = dict(os.environ)
    env.update(
        TABC_DB=dbpath,
        TABC_BUS_URL=base,
        TABC_HOME=os.path.join(tmpdir, "home"),
        TABC_DOORBELL_SPOOL=os.path.join(tmpdir, "doorbell"),
    )
    env.update(env_extra or {})
    # 🔴 Checked after the overrides, because an override is exactly how a path leaves the
    #    test directory. Naming the operating home is not the only way out: any absolute
    #    path would do, so the rule is that each one lies inside this test's directory
    #    rather than that it avoids one known directory (hu).
    root = os.path.realpath(tmpdir)
    for name in ("TABC_HOME", "TABC_DB", "TABC_DOORBELL_SPOOL"):
        if not inside(root, env[name]):
            raise ServerStartFailed(
                f"{name} is outside the directory this test owns: {env[name]} "
                f"(the test directory is {tmpdir})"
            )
    os.makedirs(env["TABC_HOME"], exist_ok=True)
    return env


def wait_until_ready(proc, port, log_path, deadline):
    """Block until the child says it bound this port. Raises rather than guessing.

    🔴 The only accepted signal is the child's own line naming the port it bound. A
    foreign listener on that port cannot produce it, and neither can a child that is still
    alive but has not bound.
    """
    while time.time() < deadline:
        if proc.poll() is not None:
            raise ServerStartFailed(
                f"tabd exited with {proc.returncode} before it bound port {port}: "
                f"{_tail(log_path)}"
            )
        found = READY.search(_read(log_path))
        if found:
            if int(found.group(1)) != port:
                raise ServerStartFailed(
                    f"tabd reported port {found.group(1)}, not the {port} it was given"
                )
            return True
        time.sleep(0.02)
    # 🔴 Two different facts, and they used to read the same. Nothing printed means the
    #    daemon never got there; lines printed that do not match means the wording moved
    #    and this module is looking for something that is no longer written (jack).
    printed = _read(log_path).strip()
    if not printed:
        raise ServerStartFailed(
            f"tabd printed nothing and never reported binding port {port} in time"
        )
    raise ServerStartFailed(
        f"tabd printed output but none of it reports binding port {port}; either it never "
        f"bound, or its announcement no longer matches what this harness looks for. "
        f"It printed: {printed[-400:]}"
    )


def start(root, tmpdir, dbpath, env_extra=None, port=None, wait=15.0):
    """Start tabd for this test. Returns (proc, port, base, log_path).

    Raises ServerStartFailed when the port belongs to the bus, when another process holds
    it, when the daemon exits, and when the daemon never reports binding it. Nothing is
    requested here, so a failure cannot reach another daemon.
    """
    port = free_port() if port is None else port
    if port in forbidden_ports():
        raise ServerStartFailed(f"port {port} belongs to the bus; a test may not use it")
    if held_by_another(port):
        raise ServerStartFailed(
            f"port {port} is already held by another process; tabd was not started and "
            "no request was sent"
        )
    base = f"http://127.0.0.1:{port}"
    env = isolated_env(tmpdir, dbpath, base, env_extra)
    # 🔴 The modules use package-relative imports, so they run as `-m tabus.x`. A
    #    subprocess finds the package through PYTHONPATH; sys.path does not cross a spawn.
    env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
    log_path = os.path.join(tmpdir, f"tabd-{port}.log")
    log = open(log_path, "wb")
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "tabus.daemon", "--port", str(port)],
            cwd=tmpdir,  # Isolate the server database from the client's working directory.
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    finally:
        # The child holds its own descriptor; this one has done its work.
        log.close()
    try:
        wait_until_ready(proc, port, log_path, time.time() + wait)
    except BaseException:
        # 🔴 Every exception, not only the one this module raises. The child exists from
        #    the moment Popen returns, so an ImportError inside the readiness step, or a
        #    Ctrl-C, would otherwise leave a daemon holding the port — and the caller's
        #    own finally cannot help, because `start` has not returned the handle yet
        #    (hu). KeyboardInterrupt and SystemExit are BaseException, which is why the
        #    clause is that wide; the original exception is re-raised untouched.
        stop(proc)
        raise
    return proc, port, base, log_path


def stop(proc, timeout=5):
    """End the daemon this test started. Safe to call more than once."""
    if proc.poll() is not None:
        return proc.returncode
    proc.send_signal(signal.SIGTERM)
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        return proc.wait(timeout=timeout)


def _read(path):
    try:
        with open(path, "rb") as fh:
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _tail(path, limit=400):
    return _read(path)[-limit:].strip() or "(no log)"
