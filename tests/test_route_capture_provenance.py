#!/usr/bin/env python3
"""A terminal route needs an inherited key bound to its exact controlling TTY."""

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tabus import route_capture


fails = []


def check(name, condition):
    print(f"  {'PASS' if condition else 'FAIL'}  {name}")
    if not condition:
        fails.append(name)


def rows(mapping):
    return lambda pid: mapping.get(pid)


def sessions(mapping):
    return lambda pid: mapping.get(pid)


# A direct agent command can have piped stdio while its CLI ancestor still owns a
# terminal.  Walking the parent chain preserves that supported route.
direct = route_capture.has_terminal_ancestor(
    start_pid=30,
    process_row=rows({30: (20, "??"), 20: (10, "??"), 10: (1, "ttys007")}),
    streams=(),
    session_id=100,
    process_session=sessions({30: 100, 20: 100, 10: 100}),
)
check("piped command accepts a terminal-attached ancestor", direct is True)

# A detached app server can carry a tab variable without any terminal process in
# its ancestry.  That variable is not enough to claim a notification route.
detached = route_capture.has_terminal_ancestor(
    start_pid=30,
    process_row=rows({30: (20, "??"), 20: (1, "??")}),
    streams=(),
    session_id=100,
    process_session=sessions({30: 100, 20: 100}),
)
check("detached runner has no terminal provenance", detached is False)

unavailable = route_capture.has_terminal_ancestor(
    start_pid=30,
    process_row=lambda _pid: None,
    streams=(),
    session_id=100,
    process_session=lambda _pid: 100,
)
check("an unavailable process lookup fails closed", unavailable is False)

cycle = route_capture.has_terminal_ancestor(
    start_pid=30,
    process_row=rows({30: (20, "??"), 20: (30, "??")}),
    streams=(),
    session_id=100,
    process_session=sessions({30: 100, 20: 100}),
)
check("a malformed parent cycle fails closed", cycle is False)

# A worker that called setsid still has its terminal launcher in the ancestry
# graph, but the launcher's terminal belongs to a different process session.
session_boundary = route_capture.has_terminal_ancestor(
    start_pid=30,
    process_row=rows({30: (20, "??"), 20: (10, "??"), 10: (1, "ttys007")}),
    streams=(),
    session_id=200,
    process_session=sessions({30: 200, 20: 100, 10: 100}),
)
check("a detached process session cannot borrow its launcher's terminal", session_boundary is False)

# A terminal frontend may still be the immediate parent after the command calls
# setsid().  Its TTY cannot prove that the detached child owns the same route.
frontend_boundary = route_capture.has_terminal_ancestor(
    start_pid=30,
    process_row=rows({30: (20, "??"), 20: (10, "ttys007")}),
    streams=(),
    session_id=200,
    process_session=sessions({20: 100}),
)
check("a terminal frontend across a session boundary is rejected", frontend_boundary is False)

missing_session = route_capture.has_terminal_ancestor(
    start_pid=30,
    process_row=rows({30: (20, "??"), 20: (10, "ttys007")}),
    streams=(),
    session_id=200,
    process_session=lambda _pid: None,
)
check("an unavailable session lookup fails closed", missing_session is False)


controlling_tty = route_capture.has_terminal_ancestor(
    start_pid=30,
    process_row=rows({30: (20, "ttys007")}),
    streams=(),
    session_id=100,
    process_session=lambda _pid: (_ for _ in ()).throw(AssertionError("getsid must not run")),
)
check("the current process controlling TTY proves attachment", controlling_tty is True)

# A detached process can retain an open terminal descriptor after setsid().
# Descriptor state is deliberately not accepted as route provenance: only the
# process table's controlling-TTY field can prove ownership.
class Interactive:
    def isatty(self):
        return True


retained_descriptor = route_capture.has_terminal_ancestor(
    start_pid=30,
    process_row=rows({30: (20, "??"), 20: (1, "ttys007")}),
    streams=(Interactive(),),
    session_id=200,
    process_session=sessions({30: 200, 20: 100}),
)
check("a retained TTY descriptor cannot bypass process-session detachment", retained_descriptor is False)

iterm_env = {"ITERM_SESSION_ID": "w0t1p0:GUID-DIRECT"}
check(
    "iTerm route is captured only when the inherited session owns this TTY",
    route_capture.capture_route(
        iterm_env,
        terminal_tty=lambda: "ttys007",
        route_tty=lambda _adapter, _target: "/dev/ttys007",
    )
    == ("iterm2", "w0t1p0:GUID-DIRECT"),
)
check(
    "a fresh PTY cannot validate a stale inherited iTerm session",
    route_capture.capture_route(
        iterm_env,
        terminal_tty=lambda: "ttys999",
        route_tty=lambda _adapter, _target: "/dev/ttys007",
    )
    == (None, None),
)
iterm_rejected = route_capture.inspect_route(
    iterm_env,
    terminal_tty=lambda: "ttys999",
    route_tty=lambda _adapter, _target: "/dev/ttys007",
)
check("a rejected inherited route is distinguishable", iterm_rejected.rejected is True)
check(
    "a rejected route keeps its exact identity for a scoped revoke",
    iterm_rejected.rejected_adapter == "iterm2"
    and iterm_rejected.rejected_target == "w0t1p0:GUID-DIRECT",
)
unresolved_iterm = route_capture.inspect_route(
    iterm_env,
    terminal_tty=lambda: "ttys007",
    route_tty=lambda _adapter, _target: None,
)
check("an unresolved iTerm target fails closed", unresolved_iterm.rejected is True)

detached_resolution_calls = []
detached_iterm = route_capture.inspect_route(
    iterm_env,
    terminal_tty=lambda: None,
    route_tty=lambda adapter, target: detached_resolution_calls.append(
        (adapter, target)
    ),
)
check("a detached inherited iTerm route is rejected", detached_iterm.rejected is True)
check(
    "no process TTY avoids launching or querying the terminal application",
    detached_resolution_calls == [],
)

tmux_env = {
    "TMUX": "/tmp/tabc-test-tmux/default,123,0",
    "TMUX_PANE": "%4",
    "ITERM_SESSION_ID": "w0t1p0:GUID-OUTER",
}
check(
    "tmux remains the owner when its pane owns the controlling TTY",
    route_capture.capture_route(
        tmux_env,
        terminal_tty=lambda: "ttys011",
        route_tty=lambda _adapter, _target: "/dev/ttys011",
    )
    == (
        "tmux",
        f"{os.path.realpath('/tmp/tabc-test-tmux/default')}\t%4",
    ),
)
check(
    "a different tmux pane TTY rejects the inherited target",
    route_capture.capture_route(
        tmux_env,
        terminal_tty=lambda: "ttys012",
        route_tty=lambda _adapter, _target: "/dev/ttys011",
    )
    == (None, None),
)

called = []
empty = route_capture.capture_route(
    {}, terminal_tty=lambda: called.append(True) or "ttys007"
)
check("no route variables returns no route", empty == (None, None))
check("no route variables avoid process inspection", called == [])
empty_decision = route_capture.inspect_route({}, terminal_tty=lambda: "ttys007")
check("no route variables are not a rejected route", empty_decision.rejected is False)

# The default resolvers must ask the terminal programs for the exact target TTY.
# These seams avoid requiring a live iTerm or tmux server in the test process.
tmux_calls = []


def fake_tmux_run(command, timeout):
    tmux_calls.append((command, timeout))
    return types.SimpleNamespace(returncode=0, stdout="/dev/ttys011\n")


resolved_tmux = route_capture.resolve_route_tty(
    "tmux", "/tmp/tmux.sock\t%4", run=fake_tmux_run
)
check("tmux target resolver returns pane_tty", resolved_tmux == "/dev/ttys011")
check(
    "tmux target resolver addresses the inherited socket and pane",
    tmux_calls
    == [
        (
            [
                "tmux",
                "-S",
                "/tmp/tmux.sock",
                "display-message",
                "-p",
                "-t",
                "%4",
                "#{pane_tty}",
            ],
            2,
        )
    ],
)

iterm_calls = []


def fake_iterm_run(command, timeout):
    iterm_calls.append((command, timeout))
    return types.SimpleNamespace(returncode=0, stdout="/dev/ttys007\n")


resolved_iterm = route_capture.resolve_route_tty(
    "iterm2", "w0t1p0:GUID-DIRECT", run=fake_iterm_run
)
check("iTerm target resolver returns the matched session TTY", resolved_iterm == "/dev/ttys007")
check(
    "iTerm target resolver asks for exactly one matching GUID",
    len(iterm_calls) == 1
    and iterm_calls[0][0][:2] == ["osascript", "-e"]
    and 'if (id of s) is "GUID-DIRECT"' in iterm_calls[0][0][2]
    and "if (count of matches) is 1" in iterm_calls[0][0][2],
)

ambiguous = route_capture.resolve_route_tty(
    "iterm2",
    "w0t1p0:GUID-DIRECT",
    run=lambda _command, _timeout: types.SimpleNamespace(
        returncode=0, stdout="/dev/ttys007\n/dev/ttys008\n"
    ),
)
check("an ambiguous target TTY result fails closed", ambiguous is None)

if fails:
    print(f"\n{len(fails)} failed: {', '.join(fails)}")
    raise SystemExit(1)
print("\nALL PASS")
