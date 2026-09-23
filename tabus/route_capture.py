"""Fail-closed capture of a terminal notification route.

Terminal environment variables are inherited by child processes.  They name a
route, but they do not prove that the process registering it still belongs to
that terminal.  A detached shared runner can therefore carry one tab's route,
allocate a fresh PTY, and still register the stale tab.  Route capture accepts
the inherited key only when its resolved TTY equals the controlling TTY of this
process or a same-session ancestor. A detached command may also use its immediate
session-boundary parent when that parent is a recognized native interactive CLI
and the foreground process-group leader of the target terminal.
"""

import os
import subprocess
import sys


_NO_TTY = {"", "?", "??", "-"}
_MAX_ANCESTORS = 64


class RouteDecision:
    """A two-value route result plus whether inherited route data was rejected."""

    __slots__ = (
        "adapter",
        "target",
        "rejected",
        "rejected_adapter",
        "rejected_target",
    )

    def __init__(
        self,
        adapter,
        target,
        rejected,
        rejected_adapter=None,
        rejected_target=None,
    ):
        self.adapter = adapter
        self.target = target
        self.rejected = rejected
        self.rejected_adapter = rejected_adapter
        self.rejected_target = rejected_target

    def __iter__(self):
        yield self.adapter
        yield self.target


def _stream_is_tty(stream):
    try:
        return bool(stream.isatty())
    except (AttributeError, OSError, ValueError):
        return False


def _process_row(pid):
    """Return ``(parent_pid, tty)`` from ps, or None when it cannot be proved."""
    try:
        result = subprocess.run(
            ["ps", "-o", "ppid=,tty=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=1,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    fields = result.stdout.strip().split(None, 1)
    if len(fields) != 2:
        return None
    try:
        parent_pid = int(fields[0])
    except ValueError:
        return None
    return parent_pid, fields[1].strip()


def _session_id(pid):
    try:
        return os.getsid(pid)
    except (AttributeError, OSError):
        return None


def terminal_tty(
    start_pid=None,
    process_row=None,
    streams=None,
    session_id=None,
    process_session=None,
    frontend_tty=None,
):
    """Return this process chain's controlling TTY, or None when unproved.

    ``ps`` is used instead of ``/proc`` so this works on both macOS and Linux.
    Any lookup failure is a negative result. The cross-session fallback is
    restricted to a checked native foreground CLI, not arbitrary ancestors.
    """
    inspect = process_row or _process_row
    inspect_session = process_session or _session_id
    # Inspect the current process before its ancestors.  A TTY file descriptor
    # can survive setsid(), while ps correctly reports that the process no
    # longer owns a controlling terminal.  Treat that mismatch as detachment,
    # not as positive route evidence.
    pid = os.getpid() if start_pid is None else start_pid
    row = inspect(pid)
    if row is None:
        return None
    try:
        parent_pid, tty = row
    except (TypeError, ValueError):
        return None
    if tty not in _NO_TTY:
        return tty
    candidates = (sys.stdin, sys.stdout, sys.stderr) if streams is None else streams
    if any(_stream_is_tty(stream) for stream in candidates):
        return None

    try:
        current_session = os.getsid(0) if session_id is None else int(session_id)
    except (AttributeError, OSError, TypeError, ValueError):
        return None

    pid = parent_pid
    seen = set()
    for _ in range(_MAX_ANCESTORS):
        if pid <= 1 or pid in seen:
            return None
        seen.add(pid)
        row = inspect(pid)
        if row is None:
            return None
        try:
            parent_pid, tty = row
        except (TypeError, ValueError):
            return None
        ancestor_session = inspect_session(pid)
        if ancestor_session is None:
            return None
        # Never cross arbitrary detached runners. Native interactive frontends
        # deliberately create detached command sessions; only that immediate
        # boundary parent may supply separately checked foreground CLI evidence.
        if ancestor_session != current_session:
            if tty not in _NO_TTY:
                from .terminal_frontend import foreground_frontend_tty
                verify_frontend = frontend_tty or foreground_frontend_tty
                return verify_frontend(pid, tty)
            return None
        if tty not in _NO_TTY:
            return tty
        pid = parent_pid
    return None


def has_terminal_ancestor(**kwargs):
    """Backward-compatible boolean view of :func:`terminal_tty`."""
    return terminal_tty(**kwargs) is not None


def _run(command, timeout):
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _one_tty(result):
    if result.returncode != 0:
        return None
    lines = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
    return lines[0] if len(lines) == 1 and lines[0] not in _NO_TTY else None


def _tmux_target_tty(target, run=None):
    try:
        socket, pane = str(target).split("\t", 1)
    except ValueError:
        return None
    if not socket or not pane:
        return None
    invoke = run or _run
    try:
        result = invoke(
            [
                "tmux",
                "-S",
                socket,
                "display-message",
                "-p",
                "-t",
                pane,
                "#{pane_tty}",
            ],
            2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return _one_tty(result)


def _iterm_target_tty(target, run=None):
    guid = str(target).split(":")[-1]
    if not guid:
        return None
    safe_guid = guid.replace("\\", "\\\\").replace('"', '\\"')
    script = f'''
tell application "iTerm2"
    set matches to {{}}
    repeat with w in windows
        repeat with t in tabs of w
            repeat with s in sessions of t
                if (id of s) is "{safe_guid}" then
                    copy s to end of matches
                end if
            end repeat
        end repeat
    end repeat
    if (count of matches) is 1 then
        return tty of item 1 of matches
    end if
    return ""
end tell
'''
    invoke = run or _run
    try:
        result = invoke(["osascript", "-e", script], 5)
    except (OSError, subprocess.SubprocessError):
        return None
    return _one_tty(result)


def resolve_route_tty(adapter, target, run=None):
    """Resolve one stored route target to the TTY it currently represents."""
    if adapter == "tmux":
        return _tmux_target_tty(target, run)
    if adapter == "iterm2":
        return _iterm_target_tty(target, run)
    return None


def _normalized_tty(value):
    if not value:
        return None
    tty = str(value).strip()
    if tty.startswith("/dev/"):
        tty = tty[5:]
    return tty if tty not in _NO_TTY else None


def inspect_route(environ=None, terminal_tty=None, route_tty=None):
    """Return the verified route and whether inherited route data was rejected."""
    env = os.environ if environ is None else environ
    tmux_pane = env.get("TMUX_PANE")
    tmux_env = env.get("TMUX")
    iterm = env.get("ITERM_SESSION_ID")

    if tmux_pane and tmux_env:
        socket = os.path.realpath(tmux_env.split(",")[0])
        candidate_adapter = "tmux"
        candidate_target = f"{socket}\t{tmux_pane}"
    elif iterm:
        candidate_adapter = "iterm2"
        candidate_target = iterm
    else:
        return RouteDecision(None, None, False)

    inspect_terminal = terminal_tty or globals()["terminal_tty"]
    resolve_target = route_tty or resolve_route_tty
    try:
        process_tty = _normalized_tty(inspect_terminal())
    except Exception:
        process_tty = None
    # No process-side TTY can never prove a terminal route. Stop before asking
    # iTerm through AppleScript: that query can launch the app or show an
    # Automation prompt even though the decision is already known to be reject.
    if not process_tty:
        return RouteDecision(
            None,
            None,
            True,
            candidate_adapter,
            candidate_target,
        )
    try:
        target_tty = _normalized_tty(resolve_target(candidate_adapter, candidate_target))
    except Exception:
        target_tty = None
    try:
        still_attached = _normalized_tty(inspect_terminal()) == process_tty
    except Exception:
        still_attached = False
    if not target_tty or process_tty != target_tty or not still_attached:
        return RouteDecision(
            None,
            None,
            True,
            candidate_adapter,
            candidate_target,
        )

    return RouteDecision(candidate_adapter, candidate_target, False)


def capture_route(environ=None, terminal_tty=None, route_tty=None):
    """Return a verified ``(adapter, target)`` pair, else ``(None, None)``."""
    decision = inspect_route(environ, terminal_tty, route_tty)
    return decision.adapter, decision.target
