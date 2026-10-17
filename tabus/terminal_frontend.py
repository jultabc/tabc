"""Verify a foreground terminal owner without a product-name allowlist.

This is local routing evidence, not an isolation boundary against a malicious
process running as the same OS user. Missing terminal evidence fails closed.
"""

import ctypes
import os
from pathlib import Path
import pwd
import re
import subprocess
import sys

from .process_argv import native_arguments


def native_executable(pid):
    if sys.platform == "darwin":
        lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        lib.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        lib.proc_pidpath.restype = ctypes.c_int
        buf = ctypes.create_string_buffer(4096)
        if lib.proc_pidpath(int(pid), buf, len(buf)) <= 0:
            return None
        return os.path.realpath(os.fsdecode(buf.value))
    if sys.platform.startswith("linux"):
        return os.path.realpath(os.readlink(f"/proc/{int(pid)}/exe"))
    return None


def executable_kind(executable, user_home):
    """Match actual native distribution paths, not argv[0] or a process title."""
    path = Path(executable)
    roots = (
        (Path(user_home) / ".codex/packages/standalone/releases", "codex"),
        (Path(user_home) / ".local/share/claude/versions", "claude"),
        (Path("/opt/homebrew/Caskroom/claude-code"), "claude-brew"),
        (Path("/opt/homebrew/Caskroom/claude-code@latest"), "claude-brew"),
        (Path("/usr/local/Caskroom/claude-code"), "claude-brew"),
        (Path("/usr/local/Caskroom/claude-code@latest"), "claude-brew"),
    )
    for root, kind in roots:
        try:
            parts = path.relative_to(root).parts
        except ValueError:
            continue
        if not parts or not re.fullmatch(r"[0-9][A-Za-z0-9.+_-]*", parts[0]):
            continue
        if kind == "codex" and len(parts) == 3 and parts[1:] == ("bin", "codex"):
            return "codex"
        if kind == "claude" and len(parts) == 1:
            return "claude"
        if kind == "claude-brew" and len(parts) == 2 and parts[1] == "claude":
            return "claude"
    return None


def interactive_arguments(kind, argv):
    """Legacy argument classifier; not used by generic route registration."""
    if not argv or os.path.basename(argv[0]) != kind:
        return False
    flags = {
        "codex": {"--no-alt-screen", "--full-auto", "--dangerously-bypass-approvals-and-sandbox", "--yolo", "--search"},
        "claude": {"--dangerously-skip-permissions", "--allow-dangerously-skip-permissions", "--continue", "-c"},
    }[kind]
    values = {
        "codex": {"-m", "--model", "-p", "--profile", "-s", "--sandbox", "-a", "--ask-for-approval", "-C", "--cd"},
        "claude": {"--model", "--permission-mode"},
    }[kind]
    args = list(argv[1:])
    if kind == "codex" and args and args[0] == "resume":
        args.pop(0)
        flags = flags | {"--last", "--all"}
        if args and not args[0].startswith("-"):
            reference = args.pop(0)
            if not _session_reference(reference):
                return False
    while args:
        arg = args.pop(0)
        option, equals, value = arg.partition("=")
        if equals and option.startswith("--"):
            if option in values and value:
                continue
            if kind == "claude" and option == "--resume" and _session_reference(value):
                continue
            return False
        if arg in flags:
            continue
        if kind == "claude" and arg in ("--resume", "-r"):
            if args and not args[0].startswith("-"):
                if not _session_reference(args.pop(0)):
                    return False
            continue
        if arg in values and args and args[0] and not args[0].startswith("-"):
            args.pop(0)
            continue
        return False
    return True


def _session_reference(value):
    # Existing interactive sessions can be resumed by name, not only UUID.
    return (re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) is not None
            and value not in {"app-server", "mcp-server", "exec", "headless"})


def process_info(pid):
    row = subprocess.run(
        ["ps", "-p", str(pid), "-o", "uid=,ppid=,pgid=,tpgid=,tty="],
        capture_output=True, text=True, timeout=1, check=False,
    )
    fields = row.stdout.split()
    if row.returncode or len(fields) != 5:
        return None
    args = native_arguments(pid)
    if not args:
        return None
    return (int(fields[0]), int(fields[1]), int(fields[2]), int(fields[3]),
            fields[4], args)


def input_terminal(pid):
    """Resolve fd 0, not inherited environment variables or a process name."""
    if sys.platform.startswith("linux"):
        return os.path.realpath(os.readlink(f"/proc/{int(pid)}/fd/0"))
    if sys.platform == "darwin":
        result = subprocess.run(
            ["/usr/sbin/lsof", "-a", "-p", str(int(pid)), "-d", "0", "-Fn"],
            capture_output=True, text=True, timeout=1, check=False,
        )
        names = [line[1:] for line in result.stdout.splitlines() if line.startswith("n")]
        if result.returncode == 0 and len(names) == 1:
            return names[0]
    return None


def foreground_frontend_tty(pid, expected_tty, inspect=None, executable=None,
                            uid=None, user_home=None, stdin_terminal=None):
    """Only the boundary parent itself may qualify; never walk past a server."""
    inspect = inspect or process_info
    executable = executable or native_executable
    stdin_terminal = stdin_terminal or input_terminal
    uid = os.getuid() if uid is None else uid
    try:
        user_home = pwd.getpwuid(uid).pw_dir if user_home is None else user_home
        before = inspect(pid)
        if before is None:
            return None
        owner, _parent, group, foreground, tty, argv = before
        if owner != uid or not (pid == group == foreground) or tty != expected_tty:
            return None
        path = executable(pid)
        # Generic terminal routing is independent of kind and installation path.
        # The immediate boundary parent must own the foreground group AND read
        # from that terminal. A pipe-backed server is not a terminal recipient.
        input_path = "/dev/" + expected_tty.removeprefix("/dev/")
        if not path or stdin_terminal(pid) != input_path:
            return None
        # Recheck mutable process state around executable and command inspection.
        if (inspect(pid) != before or executable(pid) != path
                or stdin_terminal(pid) != input_path):
            return None
        return tty
    except (OSError, ValueError, TypeError, KeyError, ctypes.ArgumentError,
            subprocess.SubprocessError):
        return None
