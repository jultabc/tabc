"""paths — where this installation keeps its state. One resolver, one answer.

🔴 Every piece of user state lives under one directory, and this module is the
only place that decides where it is. Before the package layout, state sat beside
the code and each caller derived its own path from `__file__`. That worked only
because code and state happened to share a folder. Once the code moved one level
down, fifteen separate derivations would each have followed it — putting the bus
token, the node keys and the database inside the package, and inside
site-packages for an installed copy. State is not code and does not travel with it.

Fifteen derivations are also fifteen chances to disagree. One function means the
callers cannot drift apart: a change of mind about the location is a change in
exactly one place.

Order of precedence:

  TABC_HOME   an explicit choice, absolute or relative to the working directory
  ~/.tabc    the default

🔴 The directory is created on demand, 0700 — it holds private keys and a bus
token. Creation is deliberate rather than lazy-on-write, so a permission problem
surfaces at the point of asking rather than at the first save.

Callers ask for a named file rather than joining paths themselves; that keeps
the set of state files enumerable from one place, which is what makes a
migration checkable.
"""

import os
import sys

# 🔴 The folder is .tabc, not .tabus, because the project carries two names —
#    `tabus` is the package the code imports; `tabc` is what a person types — and
#    the state folder follows the human-facing side, the same side as TABC_HOME
#    and every other TABC_ variable. A dotfolder under $HOME sits beside .ssh and
#    .config: it is read by a person, not imported. Change the value without this
#    reason and the next reader stalls here — as one of us did, reading the repo
#    and putting keys under .tabus while the running install used .tabc.
DEFAULT_DIRNAME = ".tabc"


def home() -> str:
    """The state directory for this installation. Always 0700 by the time it returns.

    🔴 Read from the environment on every call rather than cached at import, so a
    test (or a operator switching installations) can point it elsewhere without
    reloading the module. The cost is a getenv per call, which is nothing next to
    the file access that follows it.
    """
    explicit = os.environ.get("TABC_HOME", "").strip()
    path = os.path.abspath(explicit) if explicit else os.path.join(
        os.path.expanduser("~"), DEFAULT_DIRNAME
    )
    # exist_ok because several processes race here on a cold start.
    os.makedirs(path, mode=0o700, exist_ok=True)
    # 🔴 makedirs applies its mode only when it creates the directory. A home the
    #    user made first — `mkdir -p ~/.tabc` — stays at whatever umask gave it,
    #    typically 0755, and this function would have returned it while its
    #    docstring said 0700. Signing keys and the bus token live here, and a
    #    lost key cannot be re-registered, so the mode is checked every time
    #    rather than assumed from creation.
    mode = os.stat(path).st_mode & 0o777
    if mode & 0o077:
        os.chmod(path, 0o700)
        # 🔴 stderr, not stdout. A caller piping a command — `tabc who | head -1` —
        #    would otherwise get this warning as its first line of data. stderr keeps
        #    it on the screen for a person while leaving the pipe clean.
        print(
            f"tabus: tightened {path} from {mode:o} to 700 — it holds private keys",
            file=sys.stderr,
        )
    return path


def state(name: str) -> str:
    """Absolute path of one state file inside the home directory.

    🔴 Names only, never sub-paths: a caller that could pass "../x" would reach
    outside the home this module exists to define.
    """
    if not name or os.path.sep in name or name in (os.path.curdir, os.path.pardir):
        raise ValueError(f"state file name must be a bare name: {name!r}")
    return os.path.join(home(), name)
