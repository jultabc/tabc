"""Opt-in smoke from a native CLI tool: real route capture, isolated DB, no notifier.

Run directly from the agent command tool. No production registration is sent and
no text or Enter is injected into a terminal. Requires a supported live frontend.
"""
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory(prefix="tabc-native-register-") as tmp:
        env = {k: v for k, v in os.environ.items() if not k.startswith("TABC_")}
        env.pop("PYTHONPATH", None)
        env.update(TABC_HOME=tmp, TABC_DB=tmp + "/store.db", PYTHONUNBUFFERED="1")
        if "--installed" not in sys.argv:
            env["PYTHONPATH"] = str(ROOT)
        source = ("from tabus.daemon import init_extras,ThreadingHTTPServer,BusHandler;"
                  "init_extras();s=ThreadingHTTPServer(('127.0.0.1',0),BusHandler);"
                  "print(s.server_port,flush=True);s.serve_forever()")
        with open(tmp + "/server.log", "w+") as log:
            server = subprocess.Popen([sys.executable, "-c", source], cwd=tmp, env=env,
                                      stdout=log, stderr=log)
            try:
                for _ in range(100):
                    log.seek(0)
                    port_text = next(
                        (line.strip() for line in log if line.strip().isdigit()), None
                    )
                    if port_text:
                        break
                    if server.poll() is not None:
                        raise RuntimeError("isolated server exited")
                    time.sleep(.05)
                else:
                    raise RuntimeError("isolated server timeout")
                env["TABC_BUS_URL"] = "http://127.0.0.1:" + str(int(port_text))

                def register(node, extra_env=None):
                    result = subprocess.run(
                        [sys.executable, "-m", "tabus.cli", "register", "--node", node,
                         "--kind", "codex", "--auto-enter", "on"],
                        cwd=tmp, env=dict(env, **(extra_env or {})),
                        capture_output=True, text=True, timeout=20)
                    print(node, result.returncode, result.stdout.strip())
                    return result

                assert register("fresh-agent").returncode == 0
                con = sqlite3.connect(tmp + "/store.db")
                try:
                    before = con.execute("SELECT adapter,target,auto_enter,provenance_verified,revoked_at "
                                         "FROM tab_routes WHERE node_id='fresh-agent'").fetchone()
                    assert before and before[2:] == (1, 1, None), before
                    assert register("fresh-agent").returncode == 0
                    assert con.execute("SELECT adapter,target,auto_enter,provenance_verified,revoked_at "
                                       "FROM tab_routes WHERE node_id='fresh-agent'").fetchone() == before
                    other = register("second-agent")
                    assert other.returncode != 0 and "already owned" in other.stdout, other
                    assert con.execute("SELECT COUNT(*) FROM nodes WHERE node_id='second-agent'").fetchone()[0] == 0
                    bad = register("wrong-target", {"ITERM_SESSION_ID": "nonexistent-session",
                                                    "TMUX": "", "TMUX_PANE": ""})
                    assert bad.returncode == 2 and "Registration was not sent" in bad.stdout
                    assert con.execute("SELECT COUNT(*) FROM nodes WHERE node_id='wrong-target'").fetchone()[0] == 0
                    print("NATIVE FRONTEND REGISTER SMOKE PASS (no notifier started)")
                finally:
                    con.close()
            finally:
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()


if __name__ == "__main__":
    main()
