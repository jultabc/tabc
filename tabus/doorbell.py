#!/usr/bin/env python3
"""tabus.doorbell — the notification daemon. No dependency on any host application.

Why it is a separate process
----------------------------
The doorbell used to be a thread inside another program's daemon. That meant
distributing this project would ship a doorbell that only rings while that other
program is running. Letting you know about mail you have not read has no reason
to be tied to anything else.

🔴 Measured before the split: the doorbell code used nothing from its host.
   Routing was plain sqlite3, the adapters were plain subprocess calls, and the
   polling used only the standard library. It shared a process and nothing else.

Boundaries
----------
- **Reads** the message store, read-only. It never writes there; that store has
  exactly one writer and it is not this.
- **Writes** only its own ledger: rings, observations, and rounds.
- Splitting the process turned those two lines into structure. In one process
  the only thing enforcing them would be this comment.

What was deliberately left behind
---------------------------------
🔴 The tab roster and its send helper stayed with the host application, which
   still uses them for its own event routing. The doorbell no longer reading
   them is a different thing from them going away.

Observability (🔴 without this it is both independent and invisible)
-------------------------------------------------------------------
When this dies, the only symptom is silence — the failure mode this whole
project keeps running into. So:
- Every round writes one row, which proves the rounds that ran.
- 🔴 What it cannot prove: rounds that never ran. A dead process writes no row
  at all, so the freshness of that table has to be checked from outside. This
  file cannot establish that axis about itself.
- Startup and shutdown go to stdout, so it does not die quietly.

Running it
----------
    DOORBELL_MODE=LIVE python3 -m tabus.doorbell
    DOORBELL_POLL_SEC=2  (default)
    DOORBELL_MODE=SHADOW records what would have rung, and injects nothing
"""

import os
import signal
import subprocess
import sqlite3
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

# Siblings inside the package; the sys.path insert this used to need is gone.
from . import bus  # one source for what unread means and who sent it
from . import doorbell_poll  # the ledger layer
from . import lang as tabuslang  # message catalogue; t() never raises
from . import paths

# 🔴 Read-only. The message store has exactly one writer, and this daemon is not it.
#    It also honours TABC_DB now — it used to derive its own path and so kept
#    reading the default store while the bus wrote somewhere else.
TABC_DB = os.environ.get("TABC_DB", paths.state("tabus.db"))

_KST = ZoneInfo("Asia/Seoul")


def _kst():
    """Format a timestamp. 🔴 Five lines, duplicated on purpose — extracting it into
    a shared utility would restore the coupling this process just removed."""
    return datetime.now(_KST).strftime("%H:%M:%S")


# ── delivery outcomes ───────────────────────────────────────────────────
#   This axis is whether the injector acted, not whether anyone saw it. Being
#   seen is the READ state on the message itself.
#   🔴 Only success suppresses a re-ring. Everything else comes back next round —
#      counting a failure as a ring buries every message beneath it, which is
#      exactly what happened once.
DELIVER_SUCCESS = "SUCCESS"  # exactly one destination found, written, returned
DELIVER_FAILED = "FAILED"  # two or more destinations: ambiguous, so inject nowhere
DELIVER_UNKNOWN = "UNKNOWN"  # the adapter or the lookup failed: did it act? unknown
DELIVER_TEXT_ONLY = "TEXT_ONLY"  # text written, Enter failed; do not type it again
DELIVER_STALE = "STALE"  # no unread metadata remains; inject nothing
DELIVER_NOT_MINE = "NOT_MINE"  # no routing row: another machine owns it; pull covers this
DELIVER_GONE = "GONE"  # the row exists but the destination does not: the tab closed
DELIVER_UNSUPPORTED = (
    "UNSUPPORTED"  # unknown adapter. Folding this into NOT_MINE turns a typo into
    # a silent non-delivery
)


def route_for(node_id):
    """Return (adapter, target, auto_enter), or None.

    A revoked row counts as absent. Databases from before auto_enter are read as
    off so deployment order cannot enable submission.

    What this answers: where to send. Whether that destination is alive is the
    adapter's question.
    Collapsing "no row" and "destination is gone" here would produce a silent
    non-delivery again.
    """
    try:
        con = sqlite3.connect(f"file:{TABC_DB}?mode=ro", uri=True, timeout=5.0)
        try:
            columns = {row[1] for row in con.execute("PRAGMA table_info(tab_routes)")}
            if "auto_enter" in columns:
                row = con.execute(
                    "SELECT adapter, target, auto_enter FROM tab_routes"
                    " WHERE node_id=? AND revoked_at IS NULL",
                    (node_id,),
                ).fetchone()
            else:
                row = con.execute(
                    "SELECT adapter, target, 0 FROM tab_routes"
                    " WHERE node_id=? AND revoked_at IS NULL",
                    (node_id,),
                ).fetchone()
        finally:
            con.close()
        return (row[0], row[1], bool(row[2])) if row else None
    except Exception:
        # 🔴 Failing to read the table is not the same as "not mine". It is not
        # knowing, and the caller receives it as UNKNOWN.
        return "?ERROR"


def _single_line(s: str) -> str:
    """Drop C0 control characters so an auto-typed line stays one line.

    🔴 A newline in the injected text would otherwise land as a literal Enter in the
    receiving terminal — a crafted sender name carrying a command. Node ids are
    validated at registration, so this is the second layer, at the point of typing."""
    return "".join(c for c in str(s) if c >= " ")


def send_to_iterm_session(session_id, message, enter=False):
    """Inject into exactly one terminal session, identified by its session id.

    Returns (outcome, match_count).

    🔴 The target has the form 'w1t0p0:GUID'; the session id is the part after
       the colon.
    🔴 Zero matches is GONE, the tab closed. Two or more is FAILED — ambiguous,
       so nothing is injected. A failure of the automation itself is UNKNOWN.
    """
    guid = str(session_id).split(":")[-1]
    # 🔴 Strip control characters before escaping. The AppleScript escape below only
    #    handles backslash and doublequote; a newline in the message would survive
    #    into `write text` as a literal Enter in the auto-typed line. Node ids are
    #    validated at registration now, so this is defense-in-depth — the auto-typed
    #    line stays single-line whatever the input.
    safe = _single_line(message).replace("\\", "\\\\").replace('"', '\\"')
    g = guid.replace("\\", "\\\\").replace('"', '\\"')
    write_stmt = (
        'tell item 1 of matches to write text "%s" newline NO\n'
        "            delay 0.3\n"
        "            try\n"
        "                tell item 1 of matches to write text (ASCII character 13) newline NO\n"
        "            on error\n"
        "                return -2\n"
        "            end try"
        % safe
        if enter
        else 'tell item 1 of matches to write text "%s" newline NO' % safe
    )
    script = f"""
tell application "iTerm2"
    set matches to {{}}
    repeat with w in windows
        repeat with t in tabs of w
            repeat with s in sessions of t
                if (id of s) is "{g}" then
                    copy s to end of matches
                end if
            end repeat
        end repeat
    end repeat
    set n to (count of matches)
    if n = 1 then
        {write_stmt}
    end if
    return n
end tell
"""
    try:
        r = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True, timeout=15
        )
    except Exception:
        return DELIVER_UNKNOWN, -1
    if r.returncode != 0:
        return DELIVER_UNKNOWN, -1
    try:
        n = int((r.stdout or "").strip())
    except ValueError:
        return DELIVER_UNKNOWN, -1
    if n == 1:
        return DELIVER_SUCCESS, 1
    if n == -2:
        return DELIVER_TEXT_ONLY, 1
    return (DELIVER_GONE, 0) if n == 0 else (DELIVER_FAILED, n)


def send_to_tmux_pane(target, message, enter=False):
    """Inject into exactly one tmux pane, identified by socket and pane id.

    Returns (outcome, match_count).

    🔴 Why tmux: the terminal-specific adapter is macOS only. tmux runs on macOS
       and Linux both, which makes it the only cross-platform injector available
       once this project leaves one machine.
    🔴 The target is socket and pane separated by a tab. Socket paths commonly
       contain colons, so a colon cannot be the separator here.

    Failure cases, all measured:
      exit 0                                  -> SUCCESS
      exit 1, "can\'t find pane"               -> GONE, the pane closed
      exit 1, "error connecting to <socket>"  -> GONE, no server, no destination
      🔴 any other non-zero exit             -> UNKNOWN, not FAILED.
         The draft contract said a failed write is FAILED, but exit 1 covers both
         cases above *and everything else*. Putting "confirmed not written" and
         "do not know whether it was written" into one bucket is one slot with
         two meanings. What is unknown gets recorded as unknown.
      🔴 FAILED cannot occur here. A pane id is unique within a socket, so the
         ambiguous case has no way to arise. The bucket stays empty rather than
         being filled by a forced mapping.
    """
    parts = str(target).split("\t")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        # The routing value does not meet the contract. That is neither "not mine"
        # nor "no destination" — it is not knowing.
        return DELIVER_UNKNOWN, -1
    socket, pane = parts
    try:
        # 🔴 Literal mode, so the text is never interpreted as key names. Today's
        #   text contains no word like 'Enter', but this is the line that would
        #   misbehave silently on the day it does.
        r = subprocess.run(
            ["tmux", "-S", socket, "send-keys", "-t", pane, "-l", message],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if r.returncode == 0 and enter:
            try:
                r = subprocess.run(
                    ["tmux", "-S", socket, "send-keys", "-t", pane, "Enter"],
                    capture_output=True, text=True, timeout=15,
                )
            except Exception:
                return DELIVER_TEXT_ONLY, 1
            if r.returncode != 0:
                return DELIVER_TEXT_ONLY, 1
    except Exception:
        return DELIVER_UNKNOWN, -1
    if r.returncode == 0:
        return DELIVER_SUCCESS, 1
    err = (r.stderr or "").strip()
    if (
        "can't find pane" in err
        or "error connecting" in err
        or "no server running" in err
    ):
        return DELIVER_GONE, 0
    return DELIVER_UNKNOWN, -1


def unread_senders(node_id):
    """Senders of this recipient's unread mail, for the "who from" line.

    🔴 Read-only. The message store has one writer and this is not it.
    🔴 The definition of unread and the sender aggregation both live in one place
       upstream. This does not restate either.
    The doorbell still has to ring even when the names cannot be fetched.
    🔴 Failure (None) and an empty result ([]) are distinguished. Folding an
       exception into [] would make "the query failed" and "nothing yet, timing"
       produce the same message.
    """
    try:
        con = sqlite3.connect(f"file:{TABC_DB}?mode=ro", uri=True, timeout=5.0)
        con.row_factory = sqlite3.Row
        try:
            return bus.bus_unread_senders(con, node_id, include_tac=True)
        finally:
            con.close()
    except Exception as e:
        print(f"[tabus.doorbell] unread_senders lookup failed ({node_id}): {e}", flush=True)
        return None


def format_doorbell_who(senders):
    """The "who from" line: 🔴 only the most recent sender.

    It used to list every sender across all accumulated unread. That filled the
    line with old backlog and read as if all of it had just arrived. A doorbell is
    a fresh nudge — who just wrote — not an inbox summary. So: one name, the latest.

    The name needs no verification mark any more: every request is signed by the
    acting node's key and a send must come from that node, so a sender cannot be
    forged. The name shown is the authenticated sender.
    """
    if not senders:
        return ""
    return f"{senders[0]['sender']}"


def deliver_doorbell(node_id, unread):
    """Attempt one delivery. Returns (outcome, adapter, match_count).

    🔴 It writes no ledger row; that belongs to the caller.

    This is a pure branch outside the loop on purpose. Every outcome here is a
    candidate for silent non-delivery, so none of them may go untested — and out
    here, fakes for the routing and sender lookups can drive every branch.
    """
    # 🔴 No body travels. Sender and count only, as metadata.
    #    Failed or malformed sender metadata injects nothing and remains retryable.
    senders = unread_senders(node_id)
    # 🔴 None means the lookup failed; [] means it succeeded and found nothing.
    #    Return UNKNOWN for failure and STALE for an empty snapshot; neither injects.
    if senders is None:
        # Do not turn a failed lookup into a successful notification watermark.
        return DELIVER_UNKNOWN, "metadata", -1
    elif not senders:
        # The recipient may have read mail after the polling snapshot.
        return DELIVER_STALE, "metadata", 0
    else:
        latest = senders[0] if senders else None
        has_pair = (
            isinstance(latest, dict)
            and bool(latest.get("sender"))
            and latest.get("count") is not None
        )
        who = format_doorbell_who([latest] if has_pair else [])
        sender_unread = latest["count"] if has_pair else None
        # The poller's `unread` is the whole inbox and still belongs to its
        # watermark and ledger. The visible pair must describe one thing: the
        # latest sender and only that sender's unread count.
        # The old verified-count aggregate stays deliberately absent: every request
        # is signed, so verification is not a second count axis in this display.
        if who and sender_unread is not None:
            parts = []
            seen_scopes = set()
            for item in senders:
                scope = item.get("tac_id")
                if scope in seen_scopes:
                    continue
                seen_scopes.add(scope)
                key = "doorbell.ring_tac" if scope else "doorbell.ring"
                parts.append(tabuslang.t(
                    key, who=item["sender"], unread=item["count"], tac=scope,
                ))
            msg = " / ".join(parts)
        else:
            return DELIVER_UNKNOWN, "metadata", -1
    route = route_for(node_id)
    if route == "?ERROR":
        return DELIVER_UNKNOWN, "?", -1
    if route is None:
        return DELIVER_NOT_MINE, "-", 0
    adapter, target, *settings = route
    auto_enter = bool(settings[0]) if settings else False
    # 🔴 The adapter dispatch stays an if-chain. It was briefly a name-to-function
    #   table and was reverted: the table freezes function references at import
    #   time, which stopped tests from substituting them — three of sixteen failed
    #   immediately.
    #   Every branch here is a candidate for silent non-delivery, so testability
    #   costs less than the tidiness of a lookup table. Worth revisiting at five
    #   or six adapters.
    if adapter == "iterm2":  # macOS only
        result, matched = send_to_iterm_session(target, msg, enter=auto_enter)
        return result, adapter, matched
    if adapter == "tmux":  # macOS and Linux both, which is what makes distribution
    # possible
        result, matched = send_to_tmux_pane(target, msg, enter=auto_enter)
        return result, adapter, matched
    # 🔴 Folding an unknown adapter into "not mine" turns one typo into a silent
    #    non-delivery.
    return DELIVER_UNSUPPORTED, adapter, -1


_stop = False


def _on_signal(signum, _frame):
    global _stop
    _stop = True
    print(
        f"  {_kst()} [doorbell] signal {signum} — finishing this round, then stopping", flush=True
    )


def _record_start(mode):
    """🔴 Startup audit: who restarted this, why, when, and in which mode.

    It exists because the daemon kept reappearing in live mode without anyone
    knowing who had started it. Detached with nohup, the parent process is init,
    so the process table cannot answer that question — hence a ledger.

    Whoever restarts it can supply the who and why through environment
    variables. Without them, the time, mode, user, and process ids are still
    recorded.

    A failed audit never takes the doorbell down. It logs loudly and continues,
    because a silent failure is the thing being prevented."""
    import getpass
    from contextlib import closing

    try:
        with closing(doorbell_poll.ledger()) as led:
            led.execute(
                "CREATE TABLE IF NOT EXISTS starts ("
                "id INTEGER PRIMARY KEY, at TEXT NOT NULL, mode TEXT NOT NULL, "
                "pid INTEGER, ppid INTEGER, os_user TEXT, by_who TEXT, why TEXT)"
            )
            led.execute(
                "INSERT INTO starts(at, mode, pid, ppid, os_user, by_who, why)"
                " VALUES (?,?,?,?,?,?,?)",
                (
                    doorbell_poll._now(),
                    mode,
                    os.getpid(),
                    os.getppid(),
                    getpass.getuser(),
                    os.environ.get("DOORBELL_BY"),
                    os.environ.get("DOORBELL_WHY"),
                ),
            )
            led.commit()
    except Exception as e:
        print(f"  {_kst()} 🔴 [doorbell] startup audit failed, continuing: {e}", flush=True)


def main():
    interval = float(os.environ.get("DOORBELL_POLL_SEC", "2"))
    mode = os.environ.get("DOORBELL_MODE", "SHADOW").strip().upper()
    if mode not in ("SHADOW", "LIVE"):
        raise ValueError(f"unknown doorbell mode: {mode} (SHADOW or LIVE)")

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    print(
        f"  {_kst()} [doorbell] started — mode={mode} interval={interval}s\n"
        f"    reads  {TABC_DB} (read-only)\n"
        f"    writes {os.path.abspath(doorbell_poll.LEDGER_DB)}",
        flush=True,
    )
    _record_start(mode)  # 🔴 startup audit: who, why, when, which mode
    fails = 0
    while not _stop:
        try:
            for d in doorbell_poll.decide(mode):
                rcpt = d["recipient"]
                # 🔴 Observation mode injects nothing — except an announcement,
                #    which rings through the mute. The flag says whether any of this
                #    recipient's unread is one. Ordinary TAC messages respect
                #    SHADOW mode just like direct messages.
                if mode == "SHADOW" and not d.get("has_broadcast"):
                    if doorbell_poll.record_ring(
                        rcpt, d["high_id"], d["unread"], "SHADOW", "SHADOW_ONLY"
                    ):
                        print(
                            f"  {_kst()} [doorbell-SHADOW] would ring {rcpt}"
                            f" unread={d['unread']} high={d['high_id']} (nothing injected)",
                            flush=True,
                        )
                    continue
                result, adapter, matched = deliver_doorbell(rcpt, d["unread"])
                # 🔴 An announcement overriding observation mode lands here too, and
                #    the recorded mode stays as the mode it actually ran in — that is
                #    the axis the watermark is measured on. Only the log tag differs.
                # 🔴 Print only what was newly recorded. Non-suppressing outcomes
                #   become candidates again every poll, so guarding the ledger while
                #   leaving the print unguarded repeats one line at 309KB an hour.
                tag = "SHADOW-BC" if mode == "SHADOW" else "LIVE"
                if doorbell_poll.record_ring(
                    rcpt, d["high_id"], d["unread"], mode, result
                ):
                    print(
                        f"  {_kst()} [doorbell-{tag}] {rcpt} unread={d['unread']}"
                        f" -> {result} (adapter {adapter}, {matched} matched)",
                        flush=True,
                    )
            fails = 0
        except Exception as e:
            fails += 1
            # 🔴 A failure does not kill this, and it does not pass in silence
            #   either: the first one prints immediately, later ones back off.
            #   A symptomless failure is this daemon's only real risk.
            if fails == 1 or fails % 60 == 0:
                print(
                    f"  {_kst()} 🔴 [doorbell] round failed, {fails} in a row: {type(e).__name__}: {e}",
                    flush=True,
                )
        time.sleep(interval)
    print(f"  {_kst()} [doorbell] stopped", flush=True)


if __name__ == "__main__":
    main()
