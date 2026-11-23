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

import json
import math
import os
import sys
import urllib.parse

# 🔴 The folder is .tabc, not .tabus, because the project carries two names —
#    `tabus` is the package the code imports; `tabc` is what a person types — and
#    the state folder follows the human-facing side, the same side as TABC_HOME
#    and every other TABC_ variable. A dotfolder under $HOME sits beside .ssh and
#    .config: it is read by a person, not imported. Change the value without this
#    reason and the next reader stalls here — as one of us did, reading the repo
#    and putting keys under .tabus while the running install used .tabc.
DEFAULT_DIRNAME = ".tabc"

# Built-in defaults. A person changes them in config.json (see settings below),
# not here. tabd, the CLI, the MCP server, the dashboard and the doorbell read
# them only through this module.
DEFAULT_BUS_PORT = 8765
DEFAULT_BUS_URL = f"http://127.0.0.1:{DEFAULT_BUS_PORT}"
DEFAULT_DOORBELL_POLL_SEC = 2.0
MAX_DOORBELL_POLL_SEC = 86400.0  # one day; time.sleep overflows far above this
CONFIG_NAME = "config.json"
CONFIG_KEYS = ("port", "bus_url", "doorbell_poll_sec")


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


class ConfigError(ValueError):
    """config.json exists but cannot be used. The message names the file and the key."""


def config_path() -> str:
    """Where config.json would be. Does not create the home directory: reading
    settings must not have the side effect that home() has."""
    explicit = os.environ.get("TABC_HOME", "").strip()
    base = os.path.abspath(explicit) if explicit else os.path.join(os.path.expanduser("~"), DEFAULT_DIRNAME)
    return os.path.join(base, CONFIG_NAME)


def _reject_constant(name):
    raise ValueError(f"{name} is not a number JSON allows")


def _check_url(url, where):
    """An http(s) origin a client can connect to: scheme, host, valid port, nothing after.

    🔴 Checked as the callers use it: they append "/who" to the value, so a space or
    a control character (urlsplit reads "http://h:1\n" as host h, port 1)
    must be refused here, not at the first request."""
    if not isinstance(url, str):
        raise ConfigError(f"{where} must be a string")
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url):
        raise ConfigError(f"{where} must not contain spaces or control characters")
    try:
        u = urllib.parse.urlsplit(url)
        u.port  # raises ValueError for a port out of range or not a number
    except ValueError as e:
        raise ConfigError(f"{where} is not a usable URL ({e})") from None
    if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password \
            or u.path not in ("", "/") or u.query or u.fragment:
        raise ConfigError(f"{where} must be an http:// or https:// origin with a host, like {DEFAULT_BUS_URL}")
    return url


def _check_interval(sec, where):
    # 🔴 The range test comes before anything that converts to float: Python compares
    #    a huge int with a float exactly, but math.isfinite(10**400) raises
    #    OverflowError. NaN fails the range test; inf fails it too.
    if type(sec) not in (int, float) or not 0 < sec <= MAX_DOORBELL_POLL_SEC or not math.isfinite(sec):
        raise ConfigError(f"{where} must be a finite number above 0 and at most {MAX_DOORBELL_POLL_SEC:g}")
    return float(sec)


def _no_duplicate_keys(pairs):
    seen = {}
    for k, v in pairs:
        if k in seen:
            raise ValueError(f"key {k!r} appears twice")
        seen[k] = v
    return seen


def config() -> dict:
    """The settings in config.json, checked. An absent file is an empty config.

    🔴 A file that exists but is wrong stops the caller with ConfigError instead of
    falling back to the defaults: a typo in a key or a value would otherwise be
    ignored, and the program would run on a setting the person did not choose.
    The returned dict holds only checked values — never None — so a getter can use
    `key in cfg` without checking again.
    """
    path = config_path()
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise ConfigError(f"{path}: cannot read ({e.strerror})") from None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ConfigError(f"{path}: not UTF-8 ({e.reason} at byte {e.start})") from None
    try:
        # Duplicate keys are refused: json.loads would keep the last one silently.
        data = json.loads(text, parse_constant=_reject_constant, object_pairs_hook=_no_duplicate_keys)
    except ValueError as e:
        raise ConfigError(f"{path}: not JSON ({e})") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: must be a JSON object")
    unknown = sorted(set(data) - set(CONFIG_KEYS))
    if unknown:
        raise ConfigError(f"{path}: unknown key {unknown[0]!r} (known: {', '.join(CONFIG_KEYS)})")
    for key, value in data.items():
        if value is None:
            raise ConfigError(f"{path}: {key} is null; remove the key to use the default")
    if "port" in data and (type(data["port"]) is not int or not 1 <= data["port"] <= 65535):
        raise ConfigError(f"{path}: port must be an integer from 1 to 65535")
    if "bus_url" in data:
        _check_url(data["bus_url"], f"{path}: bus_url")
    if "doorbell_poll_sec" in data:
        data["doorbell_poll_sec"] = _check_interval(data["doorbell_poll_sec"], f"{path}: doorbell_poll_sec")
    return data


def bus_port() -> int:
    """The port tabd listens on when --port is not given: config.json port, else the default."""
    return config().get("port", DEFAULT_BUS_PORT)


def bus_url() -> str:
    """Where clients reach tabd. Order: TABC_BUS_URL, config.json bus_url,
    then 127.0.0.1 on bus_port() — so a port set in config.json moves tabd and
    its local clients together.

    🔴 config.json is checked first even when TABC_BUS_URL is set: a broken file
    is never ignored, whatever would have replaced the broken value (README)."""
    cfg = config()
    env = os.environ.get("TABC_BUS_URL", "").strip()
    # Without a trailing "/": callers append "/who", and tabd matches the path exactly.
    if env:
        return env.rstrip("/")
    if "bus_url" in cfg:
        return cfg["bus_url"].rstrip("/")
    return f"http://127.0.0.1:{cfg.get('port', DEFAULT_BUS_PORT)}"


def doorbell_poll_sec() -> float:
    """Seconds between doorbell rounds. Order: DOORBELL_POLL_SEC, config.json, default.
    config.json is checked first even when the variable is set (see bus_url)."""
    cfg = config()
    env = os.environ.get("DOORBELL_POLL_SEC", "").strip()
    if env:
        try:
            value = float(env)
        except ValueError:
            raise ConfigError(f"DOORBELL_POLL_SEC={env!r} is not a number") from None
        return _check_interval(value, "DOORBELL_POLL_SEC")
    return cfg.get("doorbell_poll_sec", DEFAULT_DOORBELL_POLL_SEC)
