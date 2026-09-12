"""Run with an installed wheel's Python; isolated store, no notifier or tabm."""
import os
import subprocess
import sys
import tempfile
import time


def main():
    with tempfile.TemporaryDirectory(prefix="tabc-wheel-smoke-") as tmp:
        env = {k: v for k, v in os.environ.items() if not k.startswith("TABC_")}
        for key in ("TMUX", "TMUX_PANE", "ITERM_SESSION_ID", "PYTHONPATH"):
            env.pop(key, None)
        env.update(TABC_HOME=tmp, TABC_DB=tmp + "/store.db", PYTHONUNBUFFERED="1")
        # Port 0 is allocated by the kernel. The helper prints the actual port.
        server_code = (
            "from tabus.daemon import init_extras,ThreadingHTTPServer,BusHandler;"
            "init_extras();s=ThreadingHTTPServer(('127.0.0.1',0),BusHandler);"
            "print(s.server_port,flush=True);s.serve_forever()"
        )
        with open(tmp + "/server.log", "w+") as log:
            proc = subprocess.Popen([sys.executable, "-c", server_code], cwd=tmp,
                                    env=env, stdout=log, stderr=log)
            try:
                for _ in range(100):
                    log.seek(0)
                    first = log.readline().strip()
                    if first:
                        break
                    if proc.poll() is not None:
                        raise RuntimeError("test server exited")
                    time.sleep(.05)
                else:
                    raise RuntimeError("test server startup timeout")
                env["TABC_BUS_URL"] = "http://127.0.0.1:" + str(int(first))

                def run(*args):
                    result = subprocess.run([sys.executable, "-m", "tabus.cli", *args],
                                            cwd=tmp, env=env, capture_output=True, text=True,
                                            timeout=10, check=True)
                    print("COMMAND", " ".join(args), "\n" + result.stdout)
                    return result.stdout

                for node in ("alice", "bob"):
                    run("register", "--node", node, "--kind", "codex")
                run("send", "--sender", "alice", "--to", "bob", "--subject", "Hello", "--body", "DM-body")
                assert "DM-body" in run("read", "--node", "bob")
                assert "READ" in run("sent", "--node", "alice")
                run("tac", "create", "planning", "--node", "alice")
                for node in ("alice", "bob"):
                    run("tac", "add", "planning", node, "--node", "alice")
                run("send", "--sender", "alice", "--tac", "planning", "--subject", "Plan", "--body", '\uac80\uc0c9 & 100%')
                assert '\uac80\uc0c9 & 100%' in run("tac", "search", "--node", "bob", "--query", '\uac80\uc0c9 & 100%')
                assert "1 unread" in run("dm", "--node", "bob")
                run("read", "--node", "bob")
                run("send", "--sender", "bob", "--tac", "planning", "--subject", "Reply", "--body", "reply-body")
                assert "reply-body" in run("read", "--node", "alice")
                print("INSTALLED WHEEL SMOKE PASS")
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()


if __name__ == "__main__":
    main()
