#!/usr/bin/env python3
"""
tabus v1 — a message protocol between sessions on one machine.

Agents, editors, and plain scripts all speak it.

Why it exists:
  An editor's built-in session channel only reaches sessions of that same
  product. Anything else — another vendor's agent, a script, a daemon — is
  structurally locked out.
  Broadcasting a notification and typing into a window delivers nothing when the
  other side is offline or mid-turn.
  Neither approach can answer the one question that matters: did it arrive?

━━━ Four contracts. Break one and this stops being a protocol ━━━
 1. Persist before notify.
    The wake signal goes out only after the write commits, and it carries a
    message_id and nothing else. A design that stores what it observes from the
    notification cannot structurally survive a node being offline.
 2. One delivery row per recipient.
    Delivery is at-least-once, never exactly-once. Recipients absorb duplicates
    through idempotency.
 3. Keep the stages apart — ACCEPTED / CLAIMED / INJECTED / READ / PROCESSED.
    Each guarantees something different. Collapse them into one column and you
    can no longer tell which guarantee you actually have.
 4. Old values become UNKNOWN on their own.
    A stored status is never trusted as-is. Past its TTL, the derived value
    reads as unknown. Handing back the bare word "online" lets a caller treat a
    node that died 29 seconds ago as alive, so the age always travels with it.

━━━ Prototype limits, stated rather than hidden ━━━
 * On SQLite, "server time" is really "the writing process's clock". That is
   fine on one machine. Remote participants would mean moving to Postgres.
 * The doorbell polls for unread deliveries.
 * Clients use signed HTTP requests. Local store access remains an operator
   trust boundary; see nodekey.py for request signatures.

Usage:
  tabc --help
  python3 -m tabus.bus rotate-key <node> <pubkey> --by <operator>
"""

import argparse
import hashlib
import json
import os

from . import paths
import platform
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

DB_PATH = os.environ.get(
    "TABC_DB", paths.state("tabus.db")
)
# 🔴 Two versions, two axes. __version__ is the release, in SemVer.
#    PROTOCOL_VERSION describes the message envelope on the wire. A release can
#    move without the protocol moving; the protocol only changes when the
#    envelope does, which is how old and new nodes decide whether they can talk.
#    Never merge them into one number.
#    0.x means early: before 1.0.0, SemVer allows a minor bump to break things.
#    This line is what `tabc --version` prints. It is not the single source:
#    `pyproject.toml` carries the same number for pip and PyPI, and the two
#    drifted once already — 0.1.0.post1 shipped while this said 0.1.0. Bump
#    both together; tests/test_version_agreement.py fails the build otherwise.
#    Do not copy the number into documentation.
__version__ = "0.1.1"
PROTOCOL_VERSION = "tabus.v1"

# TTLs. Past these, a derived status counts as old.
# 🔴 The point is to answer "can I send right now?" with about one second of
#    freshness. Once the status is old, the judgement it supports — that node is
#    busy, wait — no longer holds at all.
#    Heartbeats arrive about once a second, so five seconds means three or four
#    were missed. That is not online.
HEARTBEAT_ONLINE_SEC = 5  # within this, online
HEARTBEAT_STALE_SEC = 60  # within this, stale; beyond it, offline
ACTIVITY_TTL_SEC = 60  # busy/idle older than this reads as unknown

# Some agents write their own status to a file per session: busy or idle, with
# an updatedAt. It refreshes about once a second, so those nodes get freshness
# without sending heartbeats at all.
# 🔴 That file belongs to another program. A version bump can change its shape
#    or lock it. So it is only ever a secondary path: if it is missing or
#    unreadable, skip it quietly and fall back to what the node reports itself.
CLAUDE_SESSIONS_DIR = os.path.expanduser("~/.claude/sessions")
LEASE_SEC = 120  # a claim older than this becomes eligible for redelivery
MAX_ATTEMPTS = 5  # past this the delivery is DEAD, so one poison message cannot loop
MAX_BODY_BYTES = 65536

# 🔴 A display-only label. It assumes the local part equals the node_id, so the
#   registered name is also the address prefix.
#   Never store, compare, or route on it. The routing key is the node_id itself;
#   this is a nameplate, not an address book.
#   🔴 The prefix is a per-deployment setting, not baked in: TABC_DOMAIN sets it,
#   and a copy that sets nothing shows a neutral "local" rather than anyone else's
#   domain. The day someone wants per-node addresses is the day a node_id-to-address
#   table becomes necessary; until then such a table would be a second source of
#   truth for something the node_id already is.
TABC_DOMAIN = os.environ.get("TABC_DOMAIN", "local")

# 🔴 Broadcast reaches every receiving-capable node — every non-program node,
#   selected by bus_broadcast_recipients(). The code carries no names. Program
#   nodes are send-only and are dropped from the selection itself, so an
#   announcement never names one as a recipient. Narrowing a broadcast to an
#   is_team roster is a later feature: the is_team column and bus_set_team are
#   reserved for it, and nothing reads is_team today.


def _email(node_id):
    """A display label: the node_id with the domain appended, nothing more.

    Whether to show it is the caller's decision and does not belong in here.
    """
    return f"{node_id}@{TABC_DOMAIN}"


DELIVERIES_DDL = """CREATE TABLE IF NOT EXISTS deliveries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id   TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    recipient_id TEXT NOT NULL REFERENCES nodes(node_id),
    state        TEXT NOT NULL DEFAULT 'ACCEPTED'
                 CHECK (state IN ('ACCEPTED','CLAIMED','INJECTED','READ','PROCESSED',
                                  'QUARANTINED','DEAD')),
    attempts     INTEGER NOT NULL DEFAULT 0,
    lease_until  TEXT,
    claimed_by   TEXT,
    injected_at  TEXT,
    read_at      TEXT,
    processed_at TEXT,
    last_error   TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    UNIQUE (message_id, recipient_id)
);"""

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- ─── nodes: participants, and status in two layers ──────────
-- Layer 1, last_heartbeat_at: can this node still reach the store? Updated only
--   by an explicit heartbeat. Inferring it from the time of the last received
--   message cannot tell a quiet healthy node from a dead one.
-- Layer 2, activity_status: is it mid-turn? NULL means it does not report.
--   For a participant with no hook, NULL is the honest value. Guessing is not.
-- boot_at is instance fencing: if two sessions come up under one name, the one
--   that booted later wins.
CREATE TABLE IF NOT EXISTS nodes (
    node_id             TEXT PRIMARY KEY,
    instance_id         TEXT,
    boot_id             TEXT,
    boot_at             TEXT,
    kind                TEXT,
    protocol_version    TEXT NOT NULL DEFAULT 'tabus.v1',
    capabilities        TEXT NOT NULL DEFAULT '{}',
    last_heartbeat_at   TEXT NOT NULL,
    activity_status     TEXT CHECK (activity_status IN ('busy','idle')),
    activity_updated_at TEXT,
    registered_at       TEXT NOT NULL,
    -- 🔴 owner_email is the human owner of this node, in the style of git's
    --    user.email. It is self-reported.
    --    🔴 owner_email_verified separates "claimed" from "verified" as a column
    --       rather than a comment. A comment is not a guard. register performs no
    --       verification of the address, so it is always 0; a later sign-in flow
    --       is what would raise it.
    --       🔴 Consumers must read this column alongside the address. Reading the
    --          address alone means treating a self-report as established fact.
    owner_email          TEXT,
    owner_email_verified INTEGER NOT NULL DEFAULT 0,
    -- 🔴 node_pubkey is this node's ed25519 public key in base58 — the node key,
    --    and the basis for verifying signatures.
    --    First set wins: register never overwrites an existing value, because a
    --    silent overwrite is how a name gets taken over.
    --    Rotating to a different key is a separate, human-gated action.
    --    Every request is verified against this key, so identity rests on a
    --    signature rather than on a bearer token.
    node_pubkey          TEXT,
    -- 🔴 dnd is deprecated. Quiet is one concept now and snooze owns it; nothing
    --    reads this column.
    --    It is not dropped. Dropping a column from this table once crashed a live
    --    daemon, because rebuilding it re-parses these inline comments. A harmless
    --    vestige costs less than that.
    --    For quiet, use snooze_until below: indefinite is a large snooze, and
    --    "snooze off" clears it.
    dnd                  INTEGER NOT NULL DEFAULT 0,
    -- 🔴 snooze_until is timed quiet: no doorbell until this ISO instant, then it
    --    lifts on its own. NULL means not snoozed.
    --    Unlike a manual on/off flag, it expires by itself. It silences broadcast
    --    too — absolutely quiet — while delivery continues unchanged.
    snooze_until         TEXT,
    -- 🔴 is_program makes a node a send-only event source. It can authenticate
    --    and send, but no envelope may name it as a recipient. This is persisted;
    --    a missing terminal route alone cannot distinguish a program from an
    --    attended node whose adapter is unavailable.
    is_program           INTEGER NOT NULL DEFAULT 0,
    -- 🔴 is_team is reserved for a later team-scoped broadcast, where a roster
    --    narrows who receives an announcement. Nothing reads it today: a broadcast
    --    currently reaches every non-program, non-removed node (see bus_broadcast_recipients).
    --    register never touches it, following node_pubkey, so a node cannot declare
    --    itself into a future roster; setting it is an administrative action.
    is_team              INTEGER NOT NULL DEFAULT 0
);

-- ─── removed_nodes: soft-removed nodes, hidden from who, history kept ───────
-- 🔴 A separate table rather than a column on nodes. This way it arrives through
--    CREATE IF NOT EXISTS without touching the nodes DDL or its migration.
--    Soft removal is one INSERT; restoring is one DELETE.
--    A purge deletes from nodes directly, and only when the node has no history,
--    because a purge cannot be undone.
CREATE TABLE IF NOT EXISTS removed_nodes (
    node_id    TEXT PRIMARY KEY,
    removed_at TEXT NOT NULL
);

-- ─── removals: the audit log for removal and restoration ────────────────────
-- 🔴 Deletion is harder to undo than forgery, so this records who, when, what,
--    and how.
--    mode is soft, purge, or restore. removed_by is the actor, self-reported for
--    as long as a shared token is what authorizes the call.
CREATE TABLE IF NOT EXISTS removals (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id    TEXT NOT NULL,
    removed_by TEXT,
    at         TEXT NOT NULL,
    mode       TEXT NOT NULL
);

-- ─── tacs / tac_members: named group delivery ───────────────────────────────
-- 🔴 A tac is a named set of nodes. Sending to one fans out to every member:
--    one message row and N delivery rows, so each member reads and is notified
--    independently. Separate tables again, so they arrive through
--    CREATE IF NOT EXISTS without touching the nodes or deliveries DDL.
-- 🔴 Permission is enforced in exactly one place, tac_action_allowed. v1 always
--    passes; real enforcement lands there once identity exists, since refusing a
--    caller and proving who the caller is are the same problem.
--    created_by and added_by are audit traces, self-reported for now.
-- 🔴 create rejects a tac_id that collides with a node_id, so a send target is
--    never ambiguous. An explicit --tac flag is the second guard.
CREATE TABLE IF NOT EXISTS tacs (
    tac_id   TEXT PRIMARY KEY,
    label      TEXT,
    created_at TEXT NOT NULL,
    created_by TEXT,
    -- 🔴 Closing a tac is final. A closed tac cannot reopen and refuses sends.
    --    closed_at NULL means open. close_summary is fixed at the moment of
    --    closing. closed_by is the node that closed it.
    --    Continuation from an earlier tac is not a column here but the tac_links
    --    junction table, which can also express a merge of several into one.
    closed_at     TEXT,
    close_summary TEXT,
    closed_by     TEXT
);
CREATE TABLE IF NOT EXISTS tac_members (
    tac_id       TEXT NOT NULL,
    member_node_id TEXT NOT NULL,
    added_at       TEXT NOT NULL,
    added_by       TEXT,
    PRIMARY KEY (tac_id, member_node_id)   -- one membership row per member; this is where dedup comes from
);
CREATE INDEX IF NOT EXISTS idx_tac_members_grp ON tac_members(tac_id);
-- 🔴 tac_links is the junction between tacs: a child continues from a parent.
--    Direction is child to parent. A fork is several children under one parent.
--    A merge is one child with several parents.
--    A single column could not express the merge, which is why it was replaced
--    rather than kept alongside — two sources for one fact is the thing to avoid.
CREATE TABLE IF NOT EXISTS tac_links (
    child_tac  TEXT NOT NULL REFERENCES tacs(tac_id),
    parent_tac TEXT NOT NULL REFERENCES tacs(tac_id),
    linked_at  TEXT NOT NULL,
    linked_by  TEXT,
    PRIMARY KEY (child_tac, parent_tac)    -- one row per link
);
CREATE INDEX IF NOT EXISTS idx_tac_links_parent ON tac_links(parent_tac);

-- ─── messages: the authority for content ────────────────────────────────────
-- The sender mints the id first, as an idempotency key, so a retry after a
-- timeout does not store the message twice.
-- A hash of the body cannot do that job: sending the same text twice on purpose
-- is legitimate and produces the same hash.
CREATE TABLE IF NOT EXISTS messages (
    id             TEXT PRIMARY KEY,
    sender_id      TEXT NOT NULL REFERENCES nodes(node_id),
    client_ts      TEXT,
    accepted_at    TEXT NOT NULL,
    subject        TEXT NOT NULL,
    body           TEXT NOT NULL,
    body_hash      TEXT NOT NULL,
    reply_to       TEXT REFERENCES messages(id),
    thread_id      TEXT NOT NULL,
    schema_version TEXT NOT NULL DEFAULT 'tabus.v1',
    expires_at     TEXT,
    -- 🔴 Priority. This is what the protocol is actually for.
    -- Writing straight into a terminal delivers regardless of what the other
    -- side is doing, so a busy session takes everything at once and loses the
    -- thread of whatever it was working on.
    --   now   worth interrupting for: incidents, outages, anything irreversible
    --   next  read at the end of the current turn. The default, and most things.
    --   batch collect and read when idle: reference, sharing.
    -- The sender chooses, and the tool recommends based on the recipient state.
    priority       TEXT NOT NULL DEFAULT 'next'
                   CHECK (priority IN ('now','next','batch')),
    -- 🔴 tac_id records which tac this was sent to; NULL means a direct or
    --    broadcast send. It is what the tac view reads.
    --    It is a snapshot of the moment of sending. Membership can change later;
    --    the fact that this message went to that tac does not.
    tac_id        TEXT,
    -- 🔴 broadcast marks an announcement to every non-program, non-removed node other
    --    than the sender. It is a
    --    separate axis from priority: it rings through SHADOW mode, though a
    --    snoozed node stays silent. TAC messages respect SHADOW mode.
    --    Sending reuses the existing fan-out; this column is what records in the
    --    envelope that it was an announcement.
    broadcast     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id, accepted_at);
-- The index on tac_id does not belong here. It is created in migrate(), after
--   the column is added.
--   🔴 Placed here, it runs against a live database before the migration does,
--   and indexing a column that does not exist yet crashes the daemon at startup.
--   IF NOT EXISTS swallows "already there"; it does not swallow "no such column".
--   Do not move it back.

-- ─── deliveries: per-recipient delivery state ───────────────────────────────
-- UNIQUE (message_id, recipient_id) means exactly one row per recipient.
-- What each state guarantees, and nothing more:
--   ACCEPTED  written and committed. Nobody has taken it yet.
--   CLAIMED   a receiving adapter holds a lease. On expiry it is redelivered.
--   INJECTED  the delivery call succeeded. It does not mean anyone read it.
--             🔴 So it must stay in the list, and stay reopenable, until it is
--             read. INJECTED was once excluded from both the digest and pull,
--             which made it a black hole: appear on screen once, go unread, and
--             the message could never be found again. Four were stuck that way.
--             Arrival and reading are different things, and the code had treated
--             arrival as the end.
--   READ      the recipient observed this message_id in an actual turn and said
--             so. It guarantees observation, not understanding or action.
--   PROCESSED a handler recorded an outcome. Not that the outcome was correct.
--   DEAD      retries exhausted, or poison. A dead letter.
{DELIVERIES_DDL}
CREATE INDEX IF NOT EXISTS idx_deliveries_pull
    ON deliveries(recipient_id, state, created_at);

-- ─── tab_routes: the doorbell routing registry ──────────────────────────────
-- 🔴 This is the notification layer, not the bus. The bus itself — pull, open,
--   attach — works without this table entirely.
--   node_id maps to (adapter, target): which injector, and where.
--   The terminal-specific parts stay inside their adapter. This table holds
--   target as an opaque string that only the adapter interprets.
--   revoked_at is machine-set only: the route was stolen or died. NULL is
--     active. It is not a human revoke — one column, one meaning. The first
--     writer claims a target exclusively. A human off-switch would be a
--     separate column, and does not exist yet.
--   last_seen_at records the latest successful registration claim. It is not a
--   liveness lease: nothing refreshes it continuously, so age alone must never
--   revoke a route.
--   🔴 Columns for a second machine are in the target design but not here.
--      They get added when a second machine is real, not before.
CREATE TABLE IF NOT EXISTS tab_routes (
    node_id       TEXT PRIMARY KEY REFERENCES nodes(node_id),
    adapter       TEXT NOT NULL,
    target        TEXT NOT NULL,
    host_id       TEXT,
    registered_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    revoked_at    TEXT,
    provenance_verified INTEGER NOT NULL DEFAULT 0,
    auto_enter    INTEGER NOT NULL DEFAULT 0 CHECK (auto_enter IN (0, 1))
);
-- 🔴 host_id says which installation owns this route. It is a routing key, not
--    hardware identity: a UUID generated once into a local file and sent along
--    by register. OS-independent, no vendor prefix, unaffected by MAC
--    randomization.
--    When the doorbell sees a host_id that is not its own, it answers NOT_MINE
--    before attempting delivery, rather than GONE — it does not try to reach
--    another machine's route and then report it dead.
--    🔴 This proves nothing about identity. A shared token was already shown not
--       to protect the sender field; treating this as authentication would just
--       add a second field that looks like identity and is not.
-- 🔴 Exclusivity is enforced by the database, so a future writer that forgets to
--    claim the target still cannot create a duplicate.
--    🔴 The pair is (host_id, target): a target id can repeat across machines, so
--       it is only unique within one host.
--    🔴 COALESCE(host_id, ''): SQLite treats NULLs as distinct in a UNIQUE index,
--       so (NULL, same target) would slip through. Folding NULL to '' closes it.
--       The application layer already requires host_id; this is the backstop for
--       direct SQL and migrations.
-- 🔴 This unique index does not belong here either. It is created in migrate(),
--   after host_id is added. A live table predating that column would crash on
--   startup exactly as the messages index did. Do not move it back.

-- Full-text search. A ledger you cannot search later is not a ledger.
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
    USING fts5(subject, body, content='messages', content_rowid='rowid');
""".replace("{DELIVERIES_DDL}", DELIVERIES_DDL)


# ── time and database helpers ───────────────────────────────────
def local_instance():
    """An identifier for the actor.

    🔴 The axis is the session, not the process.
       What needs guarding against is not a second process but an older actor
       overwriting a newer actor's state. The thing that persists here is a
       session — one tab, one actor. A process is just a tool that session
       invokes for a moment, so it changes on every call.
       When a session exports TABC_INSTANCE, that value is used instead.

    ⚠️ Nothing is enforced yet. This only records.
       Storing who took a claim makes it possible to count, later, whether two
       different actors ever claimed under the same name.
       Deferring enforcement rests on the assumption of one session per name.
       Observing whether that assumption holds requires the record to exist
       first. An assumption you write down but never measure is not a condition.

    platform is used rather than os.uname(), which does not exist on Windows.
    """
    sess = os.environ.get("TABC_INSTANCE")
    if sess:
        return f"{platform.node()}/{sess}"
    return f"{platform.node()}:pid{os.getpid()}"


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_instant(s):
    """Normalize an ISO timestamp to UTC. A value without a zone is rejected.

    🔴 These must never be compared as strings. Two spellings of the same
       instant sort differently: a local-zone spelling can look "greater" than
       a UTC one, which once made an expiry that had already passed read as
       still in the future.
       String comparison also lets a value that is not a timestamp at all pass
       through in silence.
    """
    dt = datetime.fromisoformat(s)  # a malformed value raises ValueError
    if dt.tzinfo is None:
        raise ValueError("no timezone — there is no way to know which region this is")
    return dt.astimezone(timezone.utc)


def age_sec(ts):
    if not ts:
        return None
    try:
        return int(
            (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds()
        )
    except ValueError:
        return None


def fmt_age(sec):
    """Format an age for a person to read. A label alone hides staleness, so the
    age always travels with it."""
    if sec is None:
        return "never"
    if sec < 60:
        return f"{sec}s ago"
    if sec < 3600:
        return f"{sec // 60}m ago"
    if sec < 86400:
        return f"{sec // 3600}h ago"
    return f"{sec // 86400}d ago"


def connect():
    con = sqlite3.connect(DB_PATH, timeout=10.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=10000")
    return con


def pid_alive(pid):
    """Is the process actually alive? Used to cross-check a stale self-report.

    Only works for local participants. Returns None — meaning unknown — for
    anything remote."""
    if not pid:
        return None
    try:
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True
    except OSError:
        return None  # unsupported platform: unknown, not false


PROBE_PROBLEM = []  # why the primary path failed. Empty is normal.

ITERM_PROBE = """tell application "iTerm2"
set out to ""
repeat with w in windows
  repeat with t in tabs of w
    repeat with s in sessions of t
      set out to out & (name of s) & "\\t" & (is processing of s) & linefeed
    end repeat
  end repeat
end repeat
return out
end tell"""


def mirror_iterm(con):
    """Ask the terminal whether a tab is busy right now. This is the primary path.

    🔴 Why this beats the alternatives:
       It works at the terminal level, so it sees every participant regardless
       of which product runs inside the tab. A per-product session file reveals
       only that product's sessions, and self-reporting needs the other side to
       cooperate.
       Measured at 0.35s to query six tabs, which keeps "can I send now?" inside
       a one-second answer.

    Limits, stated plainly:
     * iTerm only. Under tmux or another terminal this path does not exist and
       the code falls back to self-reporting.
     * processing=true means the tab is busy, not that the model cannot see your
       message. Use it to avoid breaking someone's flow, not to decide whether
       delivery is possible.
     * Matching requires the tab name to equal the node_id.
    """
    mirrored = []
    try:
        import subprocess

        r = subprocess.run(
            ["osascript", "-e", ITERM_PROBE], capture_output=True, text=True, timeout=3
        )
        if r.returncode != 0:
            # Do not pass over this quietly. Record why it failed.
            # Measured: on one machine the result differs by who is calling,
            # because automation permission is granted per application. One
            # participant succeeds where another does not.
            # Swallowing that looks like "refreshing automatically" while a stale
            # value quietly stays in place.
            PROBE_PROBLEM.append(
                f"terminal query failed (code {r.returncode}): "
                f"{(r.stderr or '').strip()[:120] or 'no reason given'}"
            )
            return mirrored
    except (OSError, subprocess.SubprocessError) as e:
        PROBE_PROBLEM.append(f"could not run the terminal query: {e}")
        return mirrored

    ts = now_iso()
    for line in r.stdout.splitlines():
        if "\t" not in line:
            continue
        name, proc = line.split("\t", 1)
        name = name.strip()
        if not con.execute("SELECT 1 FROM nodes WHERE node_id=?", (name,)).fetchone():
            continue
        state = "busy" if proc.strip().lower() == "true" else "idle"
        con.execute(
            "UPDATE nodes SET activity_status=?, activity_updated_at=?, last_heartbeat_at=? "
            "WHERE node_id=?",
            (state, ts, ts, name),
        )
        mirrored.append(name)
    con.commit()
    return mirrored


def mirror_claude_sessions(con, skip=()):
    """Pull status from an agent's own session files. Secondary path.

    Those files refresh about once a second, so nodes of that product get
    freshness without sending heartbeats at all.

    🔴 The file belongs to another program and its shape can change without
       warning. On any failure this skips quietly and leaves the node's
       self-reported value alone — a secondary source must never overwrite the
       primary one.

    Returns the list of node_ids that were mirrored, so callers can tell
    automatic values apart from self-reported ones.
    """
    mirrored = []
    try:
        names = os.listdir(CLAUDE_SESSIONS_DIR)
    except OSError:
        return mirrored

    for fn in names:
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(CLAUDE_SESSIONS_DIR, fn), encoding="utf-8") as f:
                s = json.load(f)
            node = s.get("name")
            status = s.get("status")
            if not node or node in skip or status not in ("busy", "idle"):
                continue  # the primary path already covered this node
            if not con.execute(
                "SELECT 1 FROM nodes WHERE node_id=?", (node,)
            ).fetchone():
                continue  # unregistered sessions are left alone
            # Use the file mtime for freshness. A timestamp inside the file belongs to
        # another program and its format can change.
            mtime = os.path.getmtime(os.path.join(CLAUDE_SESSIONS_DIR, fn))
            ts = datetime.fromtimestamp(mtime, timezone.utc).isoformat(
                timespec="seconds"
            )
            con.execute(
                "UPDATE nodes SET activity_status=?, activity_updated_at=?, "
                "last_heartbeat_at=?, instance_id=COALESCE(instance_id,?) WHERE node_id=?",
                (status, ts, ts, str(s.get("pid") or ""), node),
            )
            mirrored.append(node)
        except (OSError, ValueError, KeyError):
            continue  # format changed, or mid-write. A secondary path skips quietly.
    con.commit()
    return mirrored


def derive_presence(row):
    """Never trust the stored value as-is. Presence is derived at read time,
    against the TTL."""
    hb = age_sec(row["last_heartbeat_at"])
    if hb is None:
        status = "unknown"
    elif hb < HEARTBEAT_ONLINE_SEC:
        status = "online"
    elif hb < HEARTBEAT_STALE_SEC:
        status = "stale"
    else:
        status = "offline"

    act_age = age_sec(row["activity_updated_at"])
    if row["activity_status"] is None:
        activity = "-"  # this participant does not report. Do not guess for it.
    elif act_age is None or act_age > ACTIVITY_TTL_SEC:
        activity = "unknown"  # a stale busy is a lie
    else:
        activity = row["activity_status"]

    return status, hb, activity


# ── commands ────────────────────────────────────────────────────
def migrate(con):
    """Bring an existing ledger up to the current schema.

    🔴 CREATE IF NOT EXISTS does not alter constraints on a table that already
       exists. Run new code against a real ledger and the quarantine UPDATE dies
       on a constraint violation, leaving the message sitting in ACCEPTED. The
       check failed, but everything looks like it passed.
       That shape — the guard is present but does not work — is worth naming,
       because reading the code proves nothing about it.

       SQLite cannot modify a constraint in place, so this rebuilds the table
       and copies the rows across.
    """
    # 🔴 Drop node_tokens: the per-node token feature was removed (the node-key
    #    signature is the sole gate now). A live database created this table with a
    #    foreign key to nodes; leaving it makes a node purge raise an integrity error
    #    on any node that still holds a token row. Before the early-return so live
    #    databases shed it. IF EXISTS is a no-op on a fresh ledger that never had it.
    con.execute("DROP TABLE IF EXISTS node_tokens")
    con.commit()
    # 🔴 key_rotations: the audit trail for an operator replacing a node's key.
    #    Before the early-return so live databases get it too. A rotation is a
    #    security event (a signing identity changed), so it is recorded, never
    #    silent.
    con.executescript(
        """CREATE TABLE IF NOT EXISTS key_rotations (
               node_id     TEXT NOT NULL,
               old_pubkey  TEXT,
               new_pubkey  TEXT NOT NULL,
               rotated_by  TEXT NOT NULL,
               at          TEXT NOT NULL
           );"""
    )
    con.commit()
    # 🔴 Add nodes.owner_email here, before the deliveries early-return below.
    #    A database that already passed that point returns immediately, so a
    #    migration placed after it would never run on exactly the installations
    #    that need it.
    #    CREATE IF NOT EXISTS cannot add a column to an existing table; ALTER can.
    #    This is idempotent — an existing column is skipped.
    #    The doorbell display reads this column, so a live database without it
    #    kills the consumer. Restarting the daemon is what adds it.
    if con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='nodes'"
    ).fetchone():
        have = {r["name"] for r in con.execute("PRAGMA table_info(nodes)")}
        if "owner_email" not in have:
            con.execute("ALTER TABLE nodes ADD COLUMN owner_email TEXT")
        if "owner_email_verified" not in have:
            con.execute(
                "ALTER TABLE nodes ADD COLUMN owner_email_verified "
                "INTEGER NOT NULL DEFAULT 0"
            )
        if "node_pubkey" not in have:
            con.execute("ALTER TABLE nodes ADD COLUMN node_pubkey TEXT")
        # 🔴 Add the dnd column. DEFAULT 0 keeps every existing node ringing.
        if "dnd" not in have:
            con.execute("ALTER TABLE nodes ADD COLUMN dnd INTEGER NOT NULL DEFAULT 0")
        # 🔴 Add snooze_until: quiet until this instant, then automatic return.
        if "snooze_until" not in have:
            con.execute("ALTER TABLE nodes ADD COLUMN snooze_until TEXT")
        # 🔴 Add the is_team column, reserved for a later team-scoped broadcast.
        #    DEFAULT 0 for every node, and nothing reads it today: a broadcast
        #    reaches every non-program, non-removed node (see bus_broadcast_recipients). No
        #    names live in the code.
        if "is_team" not in have:
            con.execute(
                "ALTER TABLE nodes ADD COLUMN is_team INTEGER NOT NULL DEFAULT 0"
            )
        # 🔴 Add is_program: program nodes are outbound-only event sources.
        #    DEFAULT 0 preserves every existing node as a valid recipient until it
        #    explicitly re-registers with --program.
        if "is_program" not in have:
            con.execute(
                "ALTER TABLE nodes ADD COLUMN is_program INTEGER NOT NULL DEFAULT 0"
            )
        con.commit()
    # 🔴 Add messages.tac_id, again before the early-return, and idempotently.
    #    New tables arrive through CREATE IF NOT EXISTS in the schema, which runs
    #    first. A new column on an existing table does not; that needs ALTER.
    #    The tac view reads this column.
    if con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'"
    ).fetchone():
        mcols = {r["name"] for r in con.execute("PRAGMA table_info(messages)")}
        if "tac_id" not in mcols:
            con.execute("ALTER TABLE messages ADD COLUMN tac_id TEXT")
        # 🔴 Add the broadcast column. DEFAULT 0 means every older message is
        #    correctly not an announcement. Same placement as tac_id, before the
        #    early-return.
        if "broadcast" not in mcols:
            con.execute(
                "ALTER TABLE messages ADD COLUMN broadcast INTEGER NOT NULL DEFAULT 0"
            )
        # 🔴 This index is created here rather than in the schema, after the
        #    column definitely exists. Sitting before the early-return means live
        #    databases reach it too.
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_tac "
            "ON messages(tac_id, accepted_at)"
        )
        con.commit()
    # 🔴 Add the closing and linking columns to tacs, idempotently, before the
    #    early-return. On a fresh database CREATE brings them along; on one that
    #    already has an older tacs table, ALTER fills the gap.
    #    Fresh and live installations diverge in exactly this way, and the
    #    surprise always arrives through deployment order.
    #    Column names are hardcoded literals, so nothing here is injectable.
    if con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tacs'"
    ).fetchone():
        tcols = {r["name"] for r in con.execute("PRAGMA table_info(tacs)")}
        for _col in ("closed_at", "close_summary", "closed_by"):
            if _col not in tcols:
                con.execute(f"ALTER TABLE tacs ADD COLUMN {_col} TEXT")
        # 🔴 The single-column link is retired in favour of the junction table,
        #    so there is one source rather than two. Dropped where it exists.
        #    Nothing reads it — it was deployed and never consumed — so this is
        #    safe.
        if "prev_tac_id" in tcols:
            # 🔴 DROP COLUMN fails on the live table with "incomplete input".
            #    SQLite rebuilds by re-parsing the stored CREATE statement, and it
            #    chokes on the inline comments in it. A reproduction against a
            #    comment-free schema passed, which is exactly why it was not
            #    caught: the reproduction was not the live schema.
            #    So this tolerates failure and leaves the column as an unused
            #    vestige. Nothing reads it, and the bus stays up. Removing it for
            #    real means rebuilding the table.
            try:
                con.execute("ALTER TABLE tacs DROP COLUMN prev_tac_id")
            except sqlite3.OperationalError:
                pass
        con.commit()
    # 🔴 Add tab_routes.host_id, before the early-return so live databases reach
    #    it. A live table predates the column.
    #    The migration covered the other tables and missed this one. register then
    #    crashed on SQL referencing host_id, which leaked a connection holding an
    #    uncommitted write, which held the WAL lock, which locked the whole bus.
    #    One missing ALTER, and every command returned "database is locked".
    if con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tab_routes'"
    ).fetchone():
        rcols = {r["name"] for r in con.execute("PRAGMA table_info(tab_routes)")}
        if "host_id" not in rcols:
            con.execute("ALTER TABLE tab_routes ADD COLUMN host_id TEXT")
        if "provenance_verified" not in rcols:
            con.execute(
                "ALTER TABLE tab_routes ADD COLUMN provenance_verified "
                "INTEGER NOT NULL DEFAULT 0"
            )
        if "auto_enter" not in rcols:
            # Automatic submission is opt-in. Existing routes become text-only
            # when this release first opens their database.
            con.execute(
                "ALTER TABLE tab_routes ADD COLUMN auto_enter "
                "INTEGER NOT NULL DEFAULT 0 CHECK (auto_enter IN (0, 1))"
            )
        # 🔴 The unique index is created here, after the column exists. In the
        #    schema it would index a column a live database does not have yet, and
        #    crash at startup.
        con.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_tab_routes_target_active "
            "ON tab_routes(COALESCE(host_id, ''), target) WHERE revoked_at IS NULL"
        )
        con.commit()
    row = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='deliveries'"
    ).fetchone()
    if not row or "QUARANTINED" in row["sql"]:
        return False
    con.executescript(f"""
        PRAGMA foreign_keys=OFF;
        BEGIN;
        ALTER TABLE deliveries RENAME TO deliveries_old;
        {DELIVERIES_DDL}
        INSERT INTO deliveries SELECT * FROM deliveries_old;
        DROP TABLE deliveries_old;
        COMMIT;
        PRAGMA foreign_keys=ON;
    """)
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_deliveries_pull "
        "ON deliveries(recipient_id, state, created_at)"
    )
    con.commit()
    return True


# ── three ways to reach unread mail ─────────────────────────────
# This is the single authority. The CLI and the daemon both use only these three.
# Merging them breaks each other's contracts, and it did:
#   leave INJECTED out of the listing and unread mail disappears from view
#   fold INJECTED into fetching and unread mail pours out on every call
# So they stay separate: listing, taking what is new, and reopening one on purpose.

UNREAD_STATES = ("ACCEPTED", "CLAIMED", "INJECTED")

# 🔴 One source for the transition table, shared by the CLI and the HTTP paths.
# INJECTED comes only from CLAIMED, READ only from INJECTED, PROCESSED only from
# READ. DEAD is reachable only through the poison rule in claim_new, never by an
# acknowledgement.
ALLOWED_DELIVERY_TRANSITIONS = {
    "INJECTED": ("CLAIMED",),
    "READ": ("INJECTED",),
    "PROCESSED": ("READ",),
}


def resolve_recipient_message_id(con, recipient, id_or_prefix):
    """Resolve a message_id within one recipient's mailbox, accepting a prefix.

    The mailbox listing shows a truncated id. Requiring the full id back meant
    that copying exactly what was displayed did not work, so people read the
    database directly instead — and a direct read sits outside the state ladder,
    which left those messages permanently unread.

    Documenting "do not do that" would not have helped. Removing the reason to
    do it does: a precondition has to live on the execution path to hold.

    Exact match first, which needs no scan and changes nothing. Failing that,
    prefix match: no row means not found, one row is the answer, two or more is
    ambiguous and rejected with the candidates listed.

    Scoped to the recipient's own mailbox, so another node's ids are never
    counted and prefix collisions across machines are harmless.

    Returns (full_id or None, error or None). When error is set, full_id is None.
    """
    # 🔴 Guard against non-strings. An integer or a list arriving from JSON makes
    #    .replace raise, which crashes the handler and leaks its connection.
    #    Reject explicitly rather than treating it as a quiet no-match.
    if not isinstance(recipient, str) or not isinstance(id_or_prefix, str):
        return None, "node and message_id must be strings"
    if not recipient or not id_or_prefix:
        return None, "node and message_id are required"
    # Exact match first: a full id passes straight through with no prefix scan.
    if con.execute(
        "SELECT 1 FROM deliveries WHERE recipient_id=? AND message_id=? LIMIT 1",
        (recipient, id_or_prefix),
    ).fetchone():
        return id_or_prefix, None
    # Prefix match. LIKE wildcards are escaped — they do not occur in ids, but
    # escaping them closes both mismatching and injection.
    esc = id_or_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = con.execute(
        "SELECT DISTINCT message_id FROM deliveries "
        "WHERE recipient_id=? AND message_id LIKE ? ESCAPE '\\' ORDER BY message_id",
        (recipient, esc + "%"),
    ).fetchall()
    if not rows:
        return (
            None,
            f"no such delivery: nothing starting with {id_or_prefix} in {recipient}'s mailbox",
        )
    if len(rows) > 1:
        cand = ", ".join(r["message_id"][:12] for r in rows[:8])
        more = "" if len(rows) <= 8 else f" and {len(rows) - 8} more"
        return None, (
            f"ambiguous: {len(rows)} messages start with {id_or_prefix} — "
            f"use more characters (candidates: {cand}{more})"
        )
    return rows[0]["message_id"], None


def _validate_envelope(con, r, now):
    """Validate the envelope. This is the third protection, and it lives in the
    helper rather than the caller.

    The checks are independent: one failing does not stop the rest from running,
    so a report names every problem instead of only the first.
    """
    bad = None
    if bad is None and r["schema_version"] != PROTOCOL_VERSION:
        bad = f"unknown schema {r['schema_version']}"
    if bad is None and r["expires_at"]:
        try:
            if parse_instant(r["expires_at"]) < datetime.now(timezone.utc):
                bad = f"expired {r['expires_at']}"
        except ValueError as e:
            bad = f"expiry is not a timestamp ({e})"
    if bad is None and hashlib.sha256(r["body"].encode()).hexdigest() != r["body_hash"]:
        bad = "body does not match its stored hash"
    return bad


def list_unread_titles(con, node, priority=None, limit=50):
    """Unread subjects. Covers all three unread states and never returns a body.

    🔴 Skipping envelope validation here is deliberate. Only subjects leave this
       function, so there is no path for a malformed body to escape, and a
       message that fails validation can still be shown as present rather than
       vanishing from the list.
    """
    ph = ",".join("?" * len(UNREAD_STATES))
    where_p = " AND m.priority=?" if priority else ""
    params = [node, *UNREAD_STATES] + ([priority] if priority else []) + [limit]
    return con.execute(
        f"""SELECT d.id AS did, d.state, m.id, m.sender_id, m.subject,
                   m.accepted_at, m.priority, length(m.body) AS len
              FROM deliveries d JOIN messages m ON m.id = d.message_id
             WHERE d.recipient_id=? AND d.state IN ({ph}){where_p}
             ORDER BY CASE m.priority WHEN 'now' THEN 0 WHEN 'next' THEN 1 ELSE 2 END,
                      m.accepted_at LIMIT ?""",
        params,
    ).fetchall()


def claim_new(con, node, limit=10, priority=None, lease_sec=None, claimed_by=None):
    """Take only what is new. The protections live here rather than in the caller.

    Order: recover expired leases, mark over-retried deliveries DEAD, select
    ACCEPTED candidates, validate the envelope, then claim conditionally so only
    the winner appears in the result.

    🔴 INJECTED must never leak out of this function. A mutation test covers it.
    """
    if is_removed(con, node):
        return (
            [],
            [],
            [],
        )  # 🔴 a removed node cannot pull; only restore brings it back
    now = now_iso()
    lease_sec = lease_sec or LEASE_SEC
    claimed_by = claimed_by or local_instance()

    # Steps 1 and 2 run before anything new is selected.
    con.execute(
        "UPDATE deliveries SET state='ACCEPTED', lease_until=NULL, "
        "claimed_by=NULL, updated_at=? "
        "WHERE recipient_id=? AND state='CLAIMED' AND lease_until < ?",
        (now, node, now),
    )
    con.execute(
        "UPDATE deliveries SET state='DEAD', updated_at=? "
        "WHERE recipient_id=? AND state='ACCEPTED' AND attempts >= ?",
        (now, node, MAX_ATTEMPTS),
    )
    con.commit()

    where_p = " AND m.priority=?" if priority else ""
    params = [node] + ([priority] if priority else []) + [limit]
    rows = con.execute(
        f"""SELECT d.id AS did, d.state, d.attempts, m.id, m.sender_id, m.subject,
                   m.body, m.accepted_at, m.thread_id, m.reply_to, m.priority,
                   m.schema_version, m.expires_at, m.body_hash
              FROM deliveries d JOIN messages m ON m.id = d.message_id
             WHERE d.recipient_id=? AND d.state='ACCEPTED'{where_p}
             ORDER BY CASE m.priority WHEN 'now' THEN 0 WHEN 'next' THEN 1 ELSE 2 END,
                      m.accepted_at LIMIT ?""",
        params,
    ).fetchall()

    out, quarantined, lost_claim = [], [], []
    lease = datetime.fromtimestamp(time.time() + lease_sec, timezone.utc).isoformat(
        timespec="seconds"
    )
    for r in rows:
        # Step 4: validate the envelope. A failure quarantines, and no body goes out.
        bad = _validate_envelope(con, r, now)
        if bad:
            con.execute(
                "UPDATE deliveries SET state='QUARANTINED', last_error=?, updated_at=? "
                "WHERE id=? AND state='ACCEPTED'",
                (bad, now, r["did"]),
            )
            quarantined.append((r["id"], bad))
            continue
        # Step 5: claim conditionally. A row that loses the lock is dropped from
        # the result rather than returned to two callers.
        won = con.execute(
            "UPDATE deliveries SET state='CLAIMED', lease_until=?, claimed_by=?, "
            "attempts=attempts+1, updated_at=? WHERE id=? AND state='ACCEPTED'",
            (lease, claimed_by, now, r["did"]),
        ).rowcount
        if not won:
            lost_claim.append(r["id"])
            continue
        rr = dict(r)
        rr["state"] = "CLAIMED"
        out.append(rr)
    con.commit()
    return out, quarantined, lost_claim


def reopen_unread(
    con, node, message_id=None, limit=10, lease_sec=None, claimed_by=None
):
    """Reopen a chosen message.

    Order: validate the envelope first; if the delivery is ACCEPTED, advance it
    to CLAIMED conditionally, and only the winner gets the body. Deliveries
    already CLAIMED or INJECTED open as they are.

    State never moves backwards, and no new delivery row is created. Those two
    are the mailbox contract.
    """
    now = now_iso()
    lease_sec = lease_sec or LEASE_SEC
    claimed_by = claimed_by or local_instance()
    ph = ",".join("?" * len(UNREAD_STATES))
    where_m = " AND m.id=?" if message_id else ""
    params = [node, *UNREAD_STATES] + ([message_id] if message_id else []) + [limit]
    rows = con.execute(
        f"""SELECT d.id AS did, d.state, d.attempts, m.id, m.sender_id, m.subject,
                   m.body, m.accepted_at, m.thread_id, m.reply_to, m.priority,
                   m.schema_version, m.expires_at, m.body_hash
              FROM deliveries d JOIN messages m ON m.id = d.message_id
             WHERE d.recipient_id=? AND d.state IN ({ph}){where_m}
             ORDER BY CASE m.priority WHEN 'now' THEN 0 WHEN 'next' THEN 1 ELSE 2 END,
                      m.accepted_at LIMIT ?""",
        params,
    ).fetchall()

    out, quarantined = [], []
    for r in rows:
        bad = _validate_envelope(con, r, now)
        if bad:
            if r["state"] == "ACCEPTED":
                con.execute(
                    "UPDATE deliveries SET state='QUARANTINED', last_error=?, updated_at=? "
                    "WHERE id=? AND state='ACCEPTED'",
                    (bad, now, r["did"]),
                )
                con.commit()
            out.append(
                {
                    "did": r["did"],
                    "id": r["id"],
                    "state": r["state"],
                    "quarantined": bad,
                    "body": None,
                }
            )
            continue
        if r["state"] == "ACCEPTED":
            # Opening without claiming sends the body out while the ledger still
            # shows the delivery as belonging to nobody.
            # This transition moves forward, so it is not a state regression:
            # claim first, then open.
            lease = datetime.fromtimestamp(
                time.time() + lease_sec, timezone.utc
            ).isoformat(timespec="seconds")
            won = con.execute(
                "UPDATE deliveries SET state='CLAIMED', lease_until=?, claimed_by=?, "
                "attempts=attempts+1, updated_at=? WHERE id=? AND state='ACCEPTED'",
                (lease, claimed_by, now, r["did"]),
            ).rowcount
            if not won:
                continue  # lost the race; it comes back next round
            r = dict(r)
            r["state"] = "CLAIMED"
        rr = dict(r)
        rr["reopened"] = rr["state"] == "INJECTED"
        out.append(rr)
        if len(out) >= limit:
            break
    con.commit()  # 🔴 the claim must commit here: without it, closing rolled it back and
    #    the state rolled back to ACCEPTED. claim_new does not show this because
    #    it commits internally — another reason for the two to have one shape.
    #    Closing after a commit is safe.
    # 🔴 Three return values, matching claim_new.
    #    Reopening has no contention, so the lost-claim list is structurally always
    #    empty here. Returning it anyway is an honest zero rather than a missing
    #    field, and it means the caller does not need to know which path it took —
    #    including on the day a fourth value is added.
    return out, quarantined, []










def refresh_presence(con):
    """Refresh presence: terminal first for every participant, then per-product
    session files, and self-reporting for whatever remains.

    Returns which path each value came from. Hiding the source would make
    automatic readings indistinguishable from self-reported ones."""
    src = {n: "iterm" for n in mirror_iterm(con)}
    for n in mirror_claude_sessions(con, skip=set(src)):
        src[n] = "claude"
    return src
























def bus_register(
    con,
    node_id,
    kind="generic",
    instance_id=None,
    boot_id=None,
    capabilities=None,
    adapter=None,
    target=None,
    host_id=None,
    email=None,
    pubkey=None,
    program=False,
    take_route=False,
    auto_enter=None,
    revoke_route=False,
    revoke_adapter=None,
    revoke_target=None,
    revoke_host_id=None,
    route_verified=False,
    result_meta=None,
):
    ts = now_iso()
    # 🔴 Validate the node id on the server, with the SAME rule the key path uses.
    #    The client's signed path already rejects an unsafe name (key_path), but a
    #    direct POST /register did not — whitespace, newlines, and ../../ all
    #    registered. A newline then rode the name into the doorbell's auto-typed
    #    line. Reusing nodekey's one predicate (not a new charset) means the server
    #    and the key path can never disagree about what a name may be.
    from . import nodekey  # nodekey does not import this module; safe, kept local

    if not nodekey.is_safe_node_id(node_id):
        return False, (
            f"invalid node id: {node_id!r} — letters, digits, dash, underscore only"
        )
    # Only a literal boolean can opt into automatic submission. A truthy string
    # such as "on" must fail rather than cross the execution boundary.
    if auto_enter is not None and type(auto_enter) is not bool:
        return False, "auto_enter must be a boolean"
    # 🔴 A removed node cannot re-register; only restore brings it back.
    #    Otherwise register succeeds for a node that who does not list, and the
    #    tools contradict each other.
    if is_removed(con, node_id):
        return False, f"removed node: {node_id} — restore it first, then register"
    boot_id = boot_id or f"{os.getpid()}-{int(time.time())}"
    inst = instance_id or local_instance()
    row = con.execute(
        "SELECT boot_at FROM nodes WHERE node_id=?", (node_id,)
    ).fetchone()
    if row and row["boot_at"] and row["boot_at"] > ts:
        return False, f"a newer instance of {node_id} exists ({row['boot_at']})"

    # Program nodes are send-only and therefore cannot own a terminal route.
    # Enforce this on the server as well as in the CLI: a direct HTTP caller must
    # not combine program mode with route fields and displace a receiving node.
    if program and (adapter or target or take_route is True or revoke_route is True or route_verified is True):
        return False, "program nodes cannot claim or take terminal routes"

    if route_verified is True and not (adapter and target and host_id):
        return False, "route_verified requires a complete terminal route"

    # A takeover without a complete route is a silent no-op. Refuse it before the
    # nodes UPSERT so the caller cannot mistake a successful registration for an
    # ownership transfer that never happened.
    if take_route is True and not (adapter and target and host_id):
        return False, (
            "--take-route requires a complete terminal route "
            "(adapter, target, and host_id)"
        )
    if auto_enter is True and not (adapter and target and host_id):
        return False, "auto_enter requires a complete terminal route"

    revoke_identity = (revoke_adapter, revoke_target, revoke_host_id)
    if revoke_route is True:
        if not all(isinstance(value, str) and value for value in revoke_identity):
            return False, (
                "revoke_route requires a complete rejected route "
                "(revoke_adapter, revoke_target, and revoke_host_id)"
            )
        if adapter or target or host_id or take_route is True:
            return False, "revoke_route cannot be combined with terminal route fields or take_route"
    elif any(revoke_identity):
        return False, "rejected route fields require revoke_route=true"

    # A terminal target can belong to only one node on one installation. Terminal
    # environment variables are inherited by child processes, so merely seeing a
    # target does not prove that this registration intends to replace its owner.
    # Refuse before changing the node row or either route. Moving ownership is an
    # explicit operation (`take_route=True`), never an automatic retry.
    #
    # BEGIN IMMEDIATE closes the gap between checking the owner and writing the new
    # claim. Otherwise two registrations can both see an empty target and race to
    # the unique index; the loser gets an opaque IntegrityError instead of this
    # route-conflict contract.
    started_route_tx = False
    route_owners = []
    # Serialize identity checks with registration writes, including route-less
    # registrations. A concurrent first registration must not bypass this guard.
    if not con.in_transaction:
        con.execute("BEGIN IMMEDIATE")
        started_route_tx = True
    existing_node = con.execute(
        "SELECT is_program FROM nodes WHERE node_id=?", (node_id,)
    ).fetchone()
    if program and existing_node is not None and not existing_node["is_program"]:
        if started_route_tx:
            con.rollback()
        return False, "an existing agent cannot become a program; register a separate program name"
    if adapter and target and host_id:
        current_route = con.execute(
            "SELECT adapter, target, host_id, provenance_verified, revoked_at "
            "FROM tab_routes WHERE node_id=?", (node_id,),
        ).fetchone()
        same_active_route = bool(
            current_route and current_route["revoked_at"] is None
            and current_route["adapter"] == adapter
            and current_route["target"] == target
            and current_route["host_id"] == host_id
        )
        # Old clients may refresh the same active identity, but cannot move or
        # reactivate a route after it has acquired terminal proof.
        if (current_route and current_route["provenance_verified"] == 1
                and route_verified is not True and not same_active_route):
            if started_route_tx:
                con.rollback()
            return False, (
                "a previously verified terminal route can be replaced only by "
                "a client with current TTY proof"
            )
        if auto_enter is None:
            prior = con.execute(
                "SELECT auto_enter FROM tab_routes WHERE node_id=? AND adapter=? "
                "AND target=? AND host_id=? AND revoked_at IS NULL",
                (node_id, adapter, target, host_id),
            ).fetchone()
            auto_enter = bool(prior["auto_enter"]) if prior else False
        route_owners = [
            r["node_id"]
            for r in con.execute(
                "SELECT node_id FROM tab_routes "
                "WHERE host_id IS ? AND target=? AND node_id!=? AND revoked_at IS NULL",
                (host_id, target, node_id),
            ).fetchall()
        ]
        # Only the literal boolean True authorizes a transfer. A truthy string such
        # as "false" must not turn malformed input into an ownership change.
        if route_owners and take_route is not True:
            if started_route_tx:
                con.rollback()
            owners = ", ".join(route_owners)
            return False, (
                f"route is already owned by another node on this pane: {owners}. "
                "Register from a different pane, or pass --take-route to move it."
            )
    con.execute(
        """INSERT INTO nodes
             (node_id, instance_id, boot_id, boot_at, kind, protocol_version,
              capabilities, last_heartbeat_at, registered_at, owner_email, node_pubkey,
              is_program)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(node_id) DO UPDATE SET
             instance_id=excluded.instance_id, boot_id=excluded.boot_id,
             boot_at=excluded.boot_at, kind=excluded.kind,
             protocol_version=excluded.protocol_version,
             capabilities=excluded.capabilities,
             last_heartbeat_at=excluded.last_heartbeat_at,
             owner_email=COALESCE(excluded.owner_email, nodes.owner_email),
             is_program=excluded.is_program,
             -- 🔴 First set wins: an existing key is kept, never overwritten.
             --    A silent replacement is how a name gets taken over.
             --    Rotation is a separate, human-gated action, so register alone
             --    cannot quietly swap the key.
             node_pubkey=COALESCE(nodes.node_pubkey, excluded.node_pubkey)""",
        (
            node_id,
            inst,
            boot_id,
            ts,
            kind,
            PROTOCOL_VERSION,
            json.dumps(capabilities or {}),
            ts,
            ts,
            email,
            pubkey,
            1 if program else 0,
        ),
    )
    # 🔴 Register a doorbell route only when both adapter and target are present.
    #    Without them — a bare shell, or a remote caller — there is no row, which
    #    reads as NOT_MINE and falls back to pull. The bus itself does not depend
    #    on this table.
    #    Re-registering is a claim that this tab is alive, so it clears
    #    revoked_at. registered_at is set once and never updated.
    routed = False
    stolen = []
    # 🔴 A route also requires host_id. An older client creating a NULL route
    #    leaves an entry nobody can attribute, and the injector can never ring it.
    #    Rejecting NULL outright drops the node to pull, which delivers, rather
    #    than to a route that silently never fires.
    if adapter and target and host_id:
        # 🔴 Claim the target exclusively. A conflicting owner reached this point
        #    only when the caller explicitly requested take_route; the default path
        #    returned before changing the node or either route.
        #    The condition excludes self, so re-registering does not revoke your
        #    own route. Capture the displaced node name for the success response.
        # 🔴 The claim is on (host_id, target). A target id repeats across
        #    machines, so scoping by host keeps the revoke inside one installation.
        #    IS rather than = keeps it NULL-safe.
        stolen = route_owners
        con.execute(
            "UPDATE tab_routes SET revoked_at=? "
            "WHERE host_id IS ? AND target=? AND node_id!=? AND revoked_at IS NULL",
            (ts, host_id, target, node_id),
        )
        con.execute(
            """INSERT INTO tab_routes
                 (node_id, adapter, target, host_id, registered_at, last_seen_at,
                  auto_enter, provenance_verified, revoked_at)
               VALUES (?,?,?,?,?,?,?,?,NULL)
               ON CONFLICT(node_id) DO UPDATE SET
                 adapter=excluded.adapter, target=excluded.target, host_id=excluded.host_id,
                 last_seen_at=excluded.last_seen_at,
                 provenance_verified=MAX(tab_routes.provenance_verified, excluded.provenance_verified),
                 auto_enter=excluded.auto_enter, revoked_at=NULL""",
            (node_id, adapter, target, host_id, ts, ts, 1 if auto_enter else 0, 1 if route_verified else 0),
        )
        routed = True
    # 🔴 --program declares that this node attaches to no terminal, so it reclaims
    #    any active route it holds from a previous tab. Without that, the bell
    #    keeps going to the old tab while the tool reports "no bell" — the tool
    #    describing itself incorrectly.
    #    Only when route-less: if an adapter was given, a fresh route was just
    #    claimed above and is not a reclaim target. Revokes only its own.
    revoked_self = 0
    preserved_route_verified = None
    if program and not adapter:
        revoked_self = con.execute(
            "UPDATE tab_routes SET revoked_at=? WHERE node_id=? AND revoked_at IS NULL",
            (ts, node_id),
        ).rowcount
    # A route-less registration can still be an opt-out for a route captured
    # earlier. Success must not leave that old route pressing Enter after the
    # caller explicitly chose off. Omission does not change a route-less setting.
    auto_enter_reset = 0
    if not (adapter and target and host_id) and auto_enter is False:
        auto_enter_reset = con.execute(
            "UPDATE tab_routes SET auto_enter=0 "
            "WHERE node_id=? AND auto_enter!=0 AND revoked_at IS NULL",
            (node_id,),
        ).rowcount
    if revoke_route is True:
        # Exact legacy identity only: a stale process must not revoke a newer
        # verified route or another target. Old unverified ABA remains a
        # mixed-version limitation until every registering client is updated.
        revoked_self = con.execute(
            "UPDATE tab_routes SET revoked_at=? "
            "WHERE node_id=? AND adapter=? AND target=? "
            "AND (host_id IS ? OR host_id IS NULL) "
            "AND provenance_verified=0 AND revoked_at IS NULL",
            (ts, node_id, revoke_adapter, revoke_target, revoke_host_id),
        ).rowcount
    active_route = con.execute(
        "SELECT provenance_verified FROM tab_routes WHERE node_id=? AND revoked_at IS NULL",
        (node_id,),
    ).fetchone()
    if revoke_route is True and active_route is not None:
        preserved_route_verified = bool(active_route["provenance_verified"])
    if result_meta is not None:
        result_meta["route_active"] = active_route is not None
        result_meta["route_provenance_verified"] = bool(active_route and active_route["provenance_verified"])
    con.commit()
    # 🔴 "No route" has two meanings and the response separates them. Created; or
    #    rejected because a tab was given without host_id, which an operator needs
    #    to know about; or simply not a tab, which is normal and uses pull.
    if routed:
        suffix = f", route={adapter}, auto_enter={'on' if auto_enter else 'off'}"
    elif (
        adapter and target
    ):  # a tab was given without host_id, so the route was refused. Silence here
    # would mean waiting forever for a bell that can never ring.
        suffix = " — 🔴 route refused: no host_id. This client is out of date, so no notification will arrive until it is updated."
    else:
        suffix = ""
    if stolen:
        suffix += f" (took over route from: {', '.join(stolen)})"
    if (
        revoked_self
    ):  # say so in the response: this registration really did remove an old route
        action = "revoked matching unverified old tab route" if revoke_route is True else "reclaimed its own old tab route"
        suffix += f" ({action}: {revoked_self})"
    elif preserved_route_verified is not None:
        proof = "verified" if preserved_route_verified else "unverified"
        suffix += f" (kept existing {proof} tab route)"
    if auto_enter_reset:
        suffix += f" (existing route auto_enter reset to off: {auto_enter_reset})"
    return True, f"registered {node_id} ({kind}){suffix}"


def bus_beat(con, node_id):
    if is_removed(con, node_id):
        return False  # 🔴 a removed node is inactive and its heartbeats are not accepted
    n = con.execute(
        "UPDATE nodes SET last_heartbeat_at=? WHERE node_id=?",
        (now_iso(), node_id),
    ).rowcount
    con.commit()
    return n > 0


def _unread_where():
    ph = ",".join("?" * len(UNREAD_STATES))
    return f"state IN ({ph})"


def bus_pending_counts(con, sender_id):
    """Per-sender pending counts for one recipient. One rule throughout: all
    three pre-read states count as pending."""
    return [
        {"recipient": r["recipient"], "pending": r["c"]}
        for r in con.execute(
            f"""SELECT d.recipient_id AS recipient, COUNT(*) AS c
                  FROM deliveries d
                 WHERE {_unread_where()}
                 GROUP BY d.recipient_id ORDER BY c DESC""",
            list(UNREAD_STATES),
        )
    ]


def bus_pending_counts_for_all(con):
    """Pending counts for every node, under the same single rule."""
    return [
        {"recipient": r["recipient"], "pending": r["c"]}
        for r in con.execute(
            f"""SELECT d.recipient_id AS recipient, COUNT(*) AS c
                  FROM deliveries d
                 WHERE {_unread_where()}
                 GROUP BY d.recipient_id ORDER BY c DESC""",
            list(UNREAD_STATES),
        )
    ]


def _nodes_has_owner_email(con):
    """Does the live nodes table have an owner_email column yet?

    Consumers have to survive the window between a migration and the code that
    depends on it. Doorbell code and the schema ship separately, so reading a
    live database that lacks the column must not crash — the deployed code and
    the running schema are two different things.
    """
    return any(
        r["name"] == "owner_email" for r in con.execute("PRAGMA table_info(nodes)")
    )


def bus_unread_senders(con, recipient, include_tac=False):
    """A recipient's unread messages grouped by sender, for the doorbell text.

    Ordered by recency. include_tac groups each conversation separately.

    🔴 Sender names are metadata — who, and how many — not content. No body ever
       travels here. That is the doorbell design, not an oversight.

    🔴 owner_email is the sender's human owner, when known. owner_email_verified
       is a separate axis from sender verification: it distinguishes a
       self-reported address from a confirmed one. The display may fold that
       distinction away; the data does not.
       🔴 The column may not exist yet on a live database mid-migration, so this
          checks first and returns None for the email rather than failing.
    """
    # 🔴 Tolerate a database without tac_id. Messages from before tacs existed are
    #    all direct, so no filter is needed for them.
    has_tac = any(
        r["name"] == "tac_id" for r in con.execute("PRAGMA table_info(messages)")
    )
    tac_filter = "AND m.tac_id IS NULL" if has_tac and not include_tac else ""
    scope = "m.tac_id" if has_tac else "NULL"
    if _nodes_has_owner_email(con):
        sql = f"""SELECT m.sender_id AS sender, {scope} AS tac_id, COUNT(*) AS c,
                         MAX(n.owner_email) AS owner_email,
                         MAX(n.owner_email_verified) AS owner_email_verified
                    FROM deliveries d JOIN messages m ON m.id = d.message_id
                    LEFT JOIN nodes n ON n.node_id = m.sender_id
                   WHERE d.recipient_id = ? AND {_unread_where()}
                     {tac_filter}
                   GROUP BY m.sender_id, {scope}
                   ORDER BY MAX(d.created_at) DESC, MAX(d.id) DESC, c DESC"""
    else:
        sql = f"""SELECT m.sender_id AS sender, {scope} AS tac_id, COUNT(*) AS c,
                         NULL AS owner_email, 0 AS owner_email_verified
                    FROM deliveries d JOIN messages m ON m.id = d.message_id
                   WHERE d.recipient_id = ? AND {_unread_where()}
                     {tac_filter}
                   GROUP BY m.sender_id, {scope}
                   ORDER BY MAX(d.created_at) DESC, MAX(d.id) DESC, c DESC"""
    return [
        {
            "sender": r["sender"],
            "tac_id": r["tac_id"],
            "count": r["c"],
            "owner_email": r["owner_email"],
            "owner_email_verified": bool(r["owner_email_verified"]),
        }
        for r in con.execute(sql, [recipient, *UNREAD_STATES])
    ]


def pending_counts_for_recipients(con, recipient_ids):
    """Pending counts for specific recipients. A thin delegation to the same
    single rule."""
    rows = dict(
        con.execute(
            f"""SELECT d.recipient_id AS r, COUNT(*) AS c
              FROM deliveries d
             WHERE {_unread_where()}
             GROUP BY d.recipient_id""",
            list(UNREAD_STATES),
        ).fetchall()
    )
    return {r: rows.get(r, 0) for r in recipient_ids}


def pending_split_by_tac_for_recipients(con, recipient_ids):
    """🔴 Split a recipient's unread into personal messages and per-tac messages.

    The two mean different things to the reader. A personal message came to you
    and it is your turn to answer. A tac message is one line added to a topic
    that fans out to every member, and it is often not your turn at all.

    For the first line of a notification to tell those apart, the payload has to
    carry the counts already split. A single number cannot express it.

    Returns {recipient: {"personal": N, "tacs": [{tac, count, last_sender,
    last_subject}, ...]}}

    Tac names and subjects are free text, so pipes and single quotes are
    replaced with '/' here. Those two characters break the notification carrier —
    one truncates the body, the other fails the send outright — and an escaping
    bug that was already fixed once should not reappear through a new field.
    """
    rows = con.execute(
        f"""SELECT r, tac, c, last_sender, last_subject FROM (
              SELECT d.recipient_id AS r, m.tac_id AS tac,
                     COUNT(*) OVER (PARTITION BY d.recipient_id, m.tac_id) AS c,
                     m.sender_id AS last_sender, m.subject AS last_subject,
                     ROW_NUMBER() OVER (PARTITION BY d.recipient_id, m.tac_id
                                        ORDER BY m.accepted_at DESC, m.rowid DESC) AS rn
                FROM deliveries d JOIN messages m ON m.id = d.message_id
               WHERE {_unread_where()}
            ) WHERE rn = 1""",
        list(UNREAD_STATES),
    ).fetchall()
    out = {r: {"personal": 0, "tacs": []} for r in recipient_ids}

    def _san(
        s,
    ):  # pipes and single quotes become '/': one is the carrier's delimiter, the
    # other its escaping hazard
        return (s or "").replace("|", "/").replace("'", "/")

    for row in rows:
        r = row["r"]
        if r not in out:
            continue
        if row["tac"] is None:
            out[r]["personal"] = row["c"]
        else:
            out[r]["tacs"].append(
                {
                    "tac": _san(row["tac"]),
                    "count": row["c"],
                    "last_sender": row["last_sender"],
                    "last_subject": _san(row["last_subject"]),
                }
            )
    return out


def doorbell_payload(sender_id, subject, recipient_counts):
    """The doorbell payload: facts only. The body is never included.

    It carries who sent it, the subject, and per-recipient pending counts.
    Nothing else.

    Pipes and single quotes become '/' because they are the carrier's delimiter
    and its escaping hazard.
    """
    subj = (subject or "").replace("|", "/").replace("'", "/")
    parts = [f"{r}: {c} queued" for r, c in sorted(recipient_counts.items())]
    body = f"[tabus] {sender_id} sent a message · " f"subject: {subj}" + (
        " · " + " / ".join(parts) if parts else ""
    )
    return body


# ── doorbell transport factory ──────────────────────────────────────────────
#   Transports swap out; the bus only knows that it notifies. Callers never see
#   which transport is in use.
#   The only transport is the file spool — no dependencies, no credentials.
#   (An internal Postgres NOTIFY transport existed once; it was removed as unused.)
#   🔴 No transport ever carries the body. Sender, subject, and pending counts.


def _doorbell_payload(recipients_csv, sender_id, subject, message_id):
    """Build the shared payload. Identical JSON regardless of transport. No body."""
    import json as _json
    import sqlite3 as _sq

    # 🔴 DB_PATH, not a second derivation of its own. This line used to compute the
    #    store's location again from __file__, which meant it ignored TABC_DB: point
    #    the bus at another database and this caller quietly stayed on the old one.
    #    One value, one home.
    bcon = _sq.connect(DB_PATH)
    counts = pending_counts_for_recipients(
        bcon, [t.strip() for t in recipients_csv.split(",") if t.strip()]
    )
    bcon.close()
    parts = " / ".join(f"{r}: {c} queued" for r, c in sorted(counts.items()))
    return _json.dumps(
        {
            "type": "TABC_DOORBELL",
            "recipient": recipients_csv,
            "message_id": message_id or "",
            "summary": f"{sender_id} sent a message · {parts}",
            "sender": sender_id,
            "subject": subject,
        },
        ensure_ascii=False,
    )


def _ring_file(payload):
    """Transport for public distribution: zero dependencies, zero credentials.

    Appends JSON Lines to a spool directory."""
    spool = os.environ.get(
        "TABC_DOORBELL_SPOOL",
        os.path.join(paths.home(), "data", "doorbell"),
    )
    os.makedirs(spool, exist_ok=True)
    path = os.path.join(spool, time.strftime("%Y%m%d") + ".jsonl")
    with open(path, "a", encoding="utf-8") as f:
        f.write(payload + "\n")


def _make_ringer():
    """The doorbell transport.

    The file spool is the only transport: zero dependencies, zero credentials.
    An internal Postgres NOTIFY transport existed once (a shared channel), but it
    was removed as unused — nothing set the transport env, the default was already
    the file spool, and it bound this tool to a Postgres instance it should not
    know about. Kept as a factory so a future transport is a one-line addition."""
    return _ring_file


def ring_doorbell(recipients_csv, sender_id, subject, message_id=None):
    """Ring the doorbell over whichever transport the factory selected.

    🔴 The body is never carried. Sender, subject, and per-recipient pending
       counts, and nothing more.
       A failure here is survivable: the write has already committed. The
       notification is a convenience; the ledger is the truth.

    Forced-failure switch: setting TABC_DOORBELL_FAIL=1 makes this always
    return failure. Acceptance and regression tests use it to reproduce the
    failure path without touching real delivery.

    message_id is the stored message's id, which correlates listener logs with
    the ledger.
    """
    if os.environ.get("TABC_DOORBELL_FAIL") == "1":
        return False, "forced failure (TABC_DOORBELL_FAIL=1)"
    try:
        payload = _doorbell_payload(recipients_csv, sender_id, subject, message_id)
        ringer = _make_ringer()  # the only transport is the file spool (see _make_ringer)
        ringer(payload)
        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, str(e)


# 🔴 A manual do-not-disturb flag was folded into snooze. Snooze subsumes it:
#    indefinite quiet is a large snooze, and clearing it is "snooze off". Two
#    columns became one, and two pairs of helpers became one.
#    🔴 The old column is not dropped — dropping a column from this table
#       re-parses its inline comments and once crashed a live daemon. It stays as
#       a deprecated vestige that nothing reads. Its helpers and endpoints are
#       gone.
def bus_set_snooze(con, node_id, minutes):
    """Silence this node for a number of minutes. Absolutely silent, broadcast
    included, and it lifts on its own when the time passes.

    🔴 Stored as a UTC ISO timestamp, on the same axis as now_iso, and compared
       through parse_instant. Never compare these as strings.

    minutes <= 0 clears it. Delivery is unaffected — silence applies only to the
    ring. Messages still land in the inbox."""
    if not con.execute("SELECT 1 FROM nodes WHERE node_id=?", (node_id,)).fetchone():
        return False, f"unregistered node: {node_id}"
    if minutes and minutes > 0:
        until = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(
            timespec="seconds"
        )
        con.execute("UPDATE nodes SET snooze_until=? WHERE node_id=?", (until, node_id))
        con.commit()
        return True, f"snoozed {minutes}m — {node_id} (until {until})"
    con.execute("UPDATE nodes SET snooze_until=NULL WHERE node_id=?", (node_id,))
    con.commit()
    return True, f"snooze cleared — {node_id}"


def bus_get_snooze(con, node_id):
    """Remaining snooze as (until_iso, seconds_left). Not snoozed or expired gives
    (None, 0); an unregistered node gives None."""
    r = con.execute(
        "SELECT snooze_until FROM nodes WHERE node_id=?", (node_id,)
    ).fetchone()
    if r is None:
        return None
    until = r["snooze_until"]
    if not until:
        return (None, 0)
    try:
        remain = (parse_instant(until) - datetime.now(timezone.utc)).total_seconds()
    except ValueError:
        return (None, 0)
    return (until, int(remain)) if remain > 0 else (None, 0)


# ── broadcast recipients: every receiving-capable node ──────────────────────
def bus_broadcast_recipients(con):
    """Broadcast recipients: every non-program node, excluding removed ones.

    🔴 A broadcast reaches everyone who can receive. Program nodes are excluded
    in the selection itself — they are send-only, and naming one as a recipient is
    refused downstream, which would fail the whole broadcast rather than skip the
    one node.

    Team-scoped broadcast — an is_team roster that narrows this set — is a later
    feature; the is_team column and bus_set_team below are reserved for it, and
    nothing reads is_team today."""
    return [
        r["node_id"]
        for r in con.execute(
            "SELECT node_id FROM nodes WHERE is_program=0 "
            "AND node_id NOT IN (SELECT node_id FROM removed_nodes) "
            "ORDER BY node_id"
        )
    ]


def bus_set_team(con, node_id, on=True):
    """Grant or revoke team membership. Administrative only.

    🔴 register cannot touch this column, so a node cannot declare itself a member
    and registering cannot escalate privilege.

    Only a registered node can be granted membership; otherwise a typo would
    create a phantom member. Returns (ok, reason)."""
    if not con.execute("SELECT 1 FROM nodes WHERE node_id=?", (node_id,)).fetchone():
        return False, f"unregistered node: {node_id} (register first)"
    con.execute(
        "UPDATE nodes SET is_team=? WHERE node_id=?", (1 if on else 0, node_id)
    )
    con.commit()
    return True, f"{node_id} is_team={1 if on else 0}"


def bus_rotate_key(con, node_id, new_pubkey, by="operator"):
    """Replace a node's public key, keeping its history. Administrative only.

    🔴 The recovery path for a lost or compromised signing key — the one thing no
    other command can do. register is COALESCE first-set-wins, so it cannot replace
    an existing key. purge could (it deletes the row), but purge refuses a node with
    any history — which is exactly the node that would need rotation. So without
    this, a used node that loses its key is locked: its new key would not match the
    registered one (so it can no longer sign as itself), and nothing can update the
    registration.

    🔴 Not a bus route, by design. The shared bus token authenticates the bus, not
    an operator; a self-serve rotate endpoint would let any client rotate any node's
    key — an identity-takeover primitive worse than the gap it closes. Rotation is a
    local operator action (running it is the human gate) and is recorded in
    key_rotations.

    The node's live token is revoked at the same time: a key rotation means the old
    credentials are no longer trusted, so a fresh one must be issued. Returns
    (ok, reason)."""
    if not new_pubkey or not isinstance(new_pubkey, str):
        return False, "new_pubkey is required"
    # 🔴 Validate the key shape before storing it. Rotation is a recovery command;
    #    a typo'd or truncated key would otherwise be accepted, the command would
    #    report success, and the node would die on its next send (its "key" cannot
    #    verify). A malformed key must fail loudly here, not silently downstream.
    #    An ed25519 public key is 32 bytes; base58 that does not decode to 32 is not
    #    a key.
    from . import nodekey  # lazy: nodekey does not import this module, but keep it local

    try:
        if len(nodekey.b58decode(new_pubkey)) != 32:
            return False, "new_pubkey is not a valid node key (must be base58 of 32 bytes)"
    except Exception:
        return False, "new_pubkey is not valid base58"
    row = con.execute(
        "SELECT node_pubkey FROM nodes WHERE node_id=?", (node_id,)
    ).fetchone()
    if row is None:
        return False, f"unregistered node: {node_id} (nothing to rotate)"
    old = row["node_pubkey"]
    if old == new_pubkey:
        return False, "new_pubkey is the same as the current key — nothing to rotate"
    con.execute(
        "UPDATE nodes SET node_pubkey=? WHERE node_id=?", (new_pubkey, node_id)
    )
    con.execute(
        "INSERT INTO key_rotations(node_id, old_pubkey, new_pubkey, rotated_by, at) "
        "VALUES(?,?,?,?,?)",
        (node_id, old, new_pubkey, by or "operator", now_iso()),
    )
    con.commit()
    short_old = (old or "(none)")[:12]
    return True, f"rotated {node_id}: pubkey {short_old}… → {new_pubkey[:12]}… by {by or 'operator'}"


def bus_send(
    con,
    sender_id,
    recipients,
    subject,
    body,
    priority="next",
    expires_at=None,
    reply_to=None,
    message_id=None,
    thread_id=None,
    tac_id=None,
    broadcast=False,
):
    if body is None or len(body.encode()) > MAX_BODY_BYTES:
        return None, "body is missing or too large"
    if expires_at:
        try:
            expires_at = parse_instant(expires_at).isoformat(timespec="seconds")
        except ValueError:
            return None, "malformed expiry"
    if not con.execute("SELECT 1 FROM nodes WHERE node_id=?", (sender_id,)).fetchone():
        return None, f"unregistered sender: {sender_id}"
    # 🔴 A removed node cannot send either, by the same test as register, beat,
    #    and pull. Otherwise it is hidden from the roster while its mail keeps
    #    arriving — one consumer of "is this node active" left unpatched.
    if is_removed(con, sender_id):
        return (
            None,
            f"a removed node cannot send: {sender_id} (restore it first)",
        )
    sender_is_program = bool(
        con.execute(
            "SELECT 1 FROM nodes WHERE node_id=? AND is_program=1", (sender_id,)
        ).fetchone()
    )
    # 🔴 A broadcast reaches every non-program node, excluding the sender and any
    #    removed node. Program nodes are dropped in the selection itself, since
    #    naming one as a recipient is refused and would fail the whole broadcast.
    #    If that leaves nobody, this refuses explicitly rather than succeeding
    #    with zero recipients.
    #    Broadcast and tac are mutually exclusive: a tac targets its members,
    #    a broadcast targets all eligible nodes. Asking for both is ambiguous, so it is
    #    refused rather than resolved by a guess.
    if broadcast and tac_id:
        return None, "broadcast and tac are mutually exclusive: an announcement goes to everyone, a tac to its members"
    if broadcast:
        recipients = [n for n in bus_broadcast_recipients(con) if n != sender_id]
    # 🔴 A tac_id fans out to the members. Permission is checked at the one seam.
    #    The sender is excluded, so nobody rings their own doorbell.
    if tac_id:
        ok, reason = tac_action_allowed(con, sender_id, tac_id, "send")
        if not ok:
            return None, reason
        if not _tac_exists(con, tac_id):
            return None, f"no such tac: {tac_id}"
        # 🔴 A closed tac accepts no further sends. Continuing means opening a new
        #    tac and linking it.
        _crow = con.execute(
            "SELECT closed_at FROM tacs WHERE tac_id=?", (tac_id,)
        ).fetchone()
        if _crow and _crow["closed_at"]:
            return (
                None,
                f"tac '{tac_id}' was closed ({_crow['closed_at']}) and accepts no sends. Open a new tac and link it.",
            )
        # 🔴 Read before send: unread messages in this tac block sending to it.
        #    Listen before speaking. Everyone sending without reading is what turns
        #    a topic into a flood.
        #    Unread means ACCEPTED or CLAIMED — not opened. Viewing the tac
        #    advances the member's unread and lifts the block.
        _unread = con.execute(
            "SELECT COUNT(*) c FROM deliveries d JOIN messages m ON m.id = d.message_id "
            "WHERE d.recipient_id = ? AND m.tac_id = ? AND d.state IN ('ACCEPTED','CLAIMED')",
            (sender_id, tac_id),
        ).fetchone()["c"]
        if _unread:
            return (
                None,
                f"tac '{tac_id}': {_unread} unread. Read them first"
                f" (tabc tac show {tac_id}). Listen before speaking.",
            )
        recipients = [m for m in bus_tac_members(con, tac_id) if m != sender_id]
        if not recipients:
            return None, f"tac '{tac_id}': no valid members once the sender is excluded"
    if recipients == ["all"]:
        # 🔴 Removed nodes are excluded from the all fan-out, or they stay hidden
        #    from the roster while still receiving group mail.
        recipients = [
            r["node_id"]
            for r in con.execute(
                "SELECT node_id FROM nodes WHERE node_id != ? "
                "AND node_id NOT IN (SELECT node_id FROM removed_nodes) ORDER BY node_id",
                (sender_id,),
            )
        ]
    # 🔴 Program nodes are send-only event sources. Reject the whole envelope
    #    before any message or delivery row is written; silently dropping just the
    #    program recipient would turn a partial send into a false success.
    program_recipients = [
        r
        for r in recipients
        if con.execute(
            "SELECT 1 FROM nodes WHERE node_id=? AND is_program=1 "
            "AND node_id NOT IN (SELECT node_id FROM removed_nodes)",
            (r,),
        ).fetchone()
    ]
    if program_recipients:
        return (
            None,
            "program nodes are send-only and cannot receive: "
            + ", ".join(program_recipients),
        )
    # 🔴 A removed node is not a valid recipient even when named directly. One
    #    place decides whether a node is active.
    valid = [
        r
        for r in recipients
        if con.execute(
            "SELECT 1 FROM nodes WHERE node_id=? "
            "AND node_id NOT IN (SELECT node_id FROM removed_nodes)",
            (r,),
        ).fetchone()
    ]
    if not valid:
        return None, "no valid recipients"

    # 🔴 The same rule extended to direct messages, scoped per recipient: sending
    #    to someone is refused while their unread messages to you are still
    #    unopened. Read that conversation first.
    #    Per recipient means only what that person sent you counts; unread mail
    #    from anyone else is irrelevant.
    #    Only the conversational axis: tac messages and broadcasts are excluded.
    #    An announcement has to be able to go out, so it never trips this gate.
    # Program nodes cannot receive or open conversational mail. Older ledgers may
    # still contain mail accepted before a node switched to --program; that stale
    # unread state must not stop its one-way event stream.
    if not tac_id and not broadcast and not sender_is_program:
        blocked = []
        for r in valid:
            n = con.execute(
                "SELECT COUNT(*) c FROM deliveries d JOIN messages m ON m.id = d.message_id "
                "WHERE d.recipient_id = ? AND m.sender_id = ? "
                "AND m.tac_id IS NULL AND m.broadcast = 0 "
                "AND d.state IN ('ACCEPTED','CLAIMED')",
                (sender_id, r),
            ).fetchone()["c"]
            if n:
                blocked.append(f"{r} ({n})")
        if blocked:
            return (
                None,
                f"read their messages first: {', '.join(blocked)}. "
                f"Open them with tabc open, then send. Listen before speaking.",
            )

    mid = message_id or str(uuid.uuid4())
    ts = now_iso()
    thread = (
        thread_id
        or (
            reply_to
            and (
                con.execute(
                    "SELECT thread_id FROM messages WHERE id=?", (reply_to,)
                ).fetchone()
                or {"thread_id": None}
            )["thread_id"]
        )
        or mid
    )
    # 🔴 The sender is authenticated at the daemon: every request is signed by the
    #    acting node's key, and /send requires the message `from` to equal that node.
    #    So the sender is already proven here, and sending as another node is refused
    #    before this point — no separate message signature is verified.
    try:
        with con:
            con.execute(
                """INSERT INTO messages
                     (id, sender_id, client_ts, accepted_at, subject, body,
                      body_hash, reply_to, thread_id, schema_version,
                      expires_at, priority, tac_id, broadcast)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    mid,
                    sender_id,
                    ts,
                    ts,
                    subject,
                    body,
                    hashlib.sha256(body.encode()).hexdigest(),
                    reply_to,
                    thread,
                    PROTOCOL_VERSION,
                    expires_at,
                    priority,
                    tac_id,
                    1 if broadcast else 0,
                ),
            )
            rowid = con.execute(
                "SELECT rowid FROM messages WHERE id=?", (mid,)
            ).fetchone()[0]
            con.execute(
                "INSERT INTO messages_fts(rowid, subject, body) VALUES (?,?,?)",
                (rowid, subject, body),
            )
            for rid in valid:
                con.execute(
                    """INSERT INTO deliveries
                         (message_id, recipient_id, state, created_at, updated_at)
                       VALUES (?,?, 'ACCEPTED', ?, ?)""",
                    (mid, rid, ts, ts),
                )
    except sqlite3.IntegrityError as e:
        if "UNIQUE" not in str(e) and "PRIMARY KEY" not in str(e):
            raise
        prev = con.execute(
            "SELECT sender_id, body_hash, subject, priority, reply_to, "
            "thread_id, expires_at FROM messages WHERE id=?",
            (mid,),
        ).fetchone()
        prev_to = sorted(
            r["recipient_id"]
            for r in con.execute(
                "SELECT recipient_id FROM deliveries WHERE message_id=?", (mid,)
            )
        )
        same = (
            prev
            and prev["sender_id"] == sender_id
            and prev["body_hash"] == hashlib.sha256(body.encode()).hexdigest()
            and prev["subject"] == subject
            and prev["priority"] == priority
            and prev["reply_to"] == reply_to
            and prev["thread_id"] == thread
            and prev["expires_at"] == expires_at
            and prev_to == sorted(valid)
        )
        if same:
            return mid, "identical envelope resent — not stored twice"
        return None, f"conflict: id={mid} already holds different content"

    # TAC deliveries notify their recipients just like direct messages.
    # 🔴 Ring exactly once, after the write commits, with no body. Pending counts
    #    come from the ledger per recipient, through the same function the CLI uses.
    #    🔴 The return value is checked, and the response carries doorbell_ok and
    #       doorbell_err. A notification failing on the remote path would otherwise
    #       vanish silently. No data is lost either way — the ledger is the truth —
    #       but "I sent it, why did it not arrive" needs something to compare.
    ok, err = ring_doorbell(",".join(valid), sender_id, subject, message_id=mid)
    return mid, {"recipients": valid, "doorbell_ok": ok, "doorbell_err": err}


def bus_mark_injected(con, node_id, message_ids):
    """Move ACCEPTED rows that went out in a response to INJECTED — the arrival
    layer.

    This is not READ. The message stays in the list until it is explicitly
    acknowledged, which is the mailbox contract.
    """
    for mid in message_ids:
        cur = con.execute(
            """UPDATE deliveries SET state='INJECTED', injected_at=?, updated_at=?,
                   attempts=attempts+1
                 WHERE recipient_id=? AND message_id=? AND state='ACCEPTED'""",
            (now_iso(), now_iso(), node_id, mid),
        )
    con.commit()


def bus_inject_by_did(con, node_id, dids):
    """Record arrival: CLAIMED to INJECTED.

    An adapter calls this after it has actually placed a row that went out over
    HTTP into its destination.

    READ is never set automatically. Appearing on a screen and being read are
    different layers.
    """
    now = now_iso()
    n = 0
    for did in dids:
        n += con.execute(
            "UPDATE deliveries SET state='INJECTED', injected_at=?, updated_at=? "
            "WHERE id=? AND recipient_id=? AND state='CLAIMED'",
            (now, now, did, node_id),
        ).rowcount
    con.commit()
    return n


def bus_ack(con, node_id, message_id, state):
    """An explicit state transition. Never backwards; re-asserting the same state
    is idempotent and allowed.

    🔴 One source for the transition table: ALLOWED_DELIVERY_TRANSITIONS. The
       CLI path and the HTTP path both read it, so they cannot drift apart.
    """
    allowed = ALLOWED_DELIVERY_TRANSITIONS
    if state not in allowed:
        return False, f"unknown state {state}"
    row = con.execute(
        """SELECT state FROM deliveries
            WHERE recipient_id=? AND message_id=?""",
        (node_id, message_id),
    ).fetchone()
    if not row:
        return False, "no such delivery row"
    cur_state = row["state"]
    if cur_state == state:
        return True, "idempotent — already in that state"
    if cur_state not in allowed[state]:
        return False, f"cannot move from {cur_state} to {state}"
    col = {"INJECTED": "injected_at", "READ": "read_at", "PROCESSED": "processed_at"}[
        state
    ]
    con.execute(
        f"""UPDATE deliveries SET state=?, {col}=?, updated_at=?
             WHERE recipient_id=? AND message_id=?""",
        (state, now_iso(), now_iso(), node_id, message_id),
    )
    con.commit()
    return True, "OK"


def bus_who_rows(con):
    refresh_presence(con)
    # 🔴 Soft-removed nodes are hidden from the roster. Their history remains.
    return [
        dict(r)
        for r in con.execute(
            "SELECT * FROM nodes "
            "WHERE node_id NOT IN (SELECT node_id FROM removed_nodes) "
            "ORDER BY node_id"
        ).fetchall()
    ]


def is_removed(con, node_id):
    """Has this node been soft-removed?

    Every place that decides whether a node is active reads this one function.

    🔴 Soft removal means fully inactive: blocked in who, send, register, beat,
       pull, and the doorbell alike. Miss one of those and the tools contradict
       each other — register works, who does not list it, send still delivers —
       which is worse than either behaviour alone.
    """
    return (
        con.execute(
            "SELECT 1 FROM removed_nodes WHERE node_id=?", (node_id,)
        ).fetchone()
        is not None
    )


def bus_remove(con, node_id, purge=False, by=None):
    """Remove a node. Soft by default.

    Soft means fully inactive — blocked in who, send, register, beat, pull, and
    the doorbell — while all history is preserved.

    --purge deletes outright, and only when the node has no history at all: no
    messages sent, none received, no routes. Any history and it refuses, because
    a purge is not undoable.

    🔴 Only restore reverses a soft removal. A hard delete reverses nothing.
    🔴 Deletion is harder to undo than forgery, so the actor is recorded in the
       removals audit.

    Returns (ok, msg).
    """
    if not node_id:
        return False, "node is required"
    if not con.execute("SELECT 1 FROM nodes WHERE node_id=?", (node_id,)).fetchone():
        return False, f"unregistered node: {node_id}"
    sent = con.execute(
        "SELECT COUNT(*) c FROM messages WHERE sender_id=?", (node_id,)
    ).fetchone()["c"]
    recv = con.execute(
        "SELECT COUNT(*) c FROM deliveries WHERE recipient_id=?", (node_id,)
    ).fetchone()["c"]
    routes = con.execute(
        "SELECT COUNT(*) c FROM tab_routes WHERE node_id=?", (node_id,)
    ).fetchone()["c"]
    if purge:
        if sent or recv or routes:
            return False, (
                f"purge refused: {node_id} has history (sent {sent} / received {recv} / routes {routes}). "
                "Purge is only for a node with none. To hide it instead, drop --purge."
            )
        con.execute("DELETE FROM removed_nodes WHERE node_id=?", (node_id,))
        # 🔴 A purge removes everything belonging to this node — as much as the word
        #    purge promises, or the name is wrong. Mailbox open records carry no key,
        #    but leaving them attaches an old node's history to whoever registers that
        #    name next. This is operational state rather than message history, and a
        #    purge only happens when there is no history at all.
        for _tbl, _col in (("mailbox_opens", "node"),):
            try:
                con.execute(f"DELETE FROM {_tbl} WHERE {_col}=?", (node_id,))
            except sqlite3.OperationalError:
                pass  # a ledger without that table, as in some tests. Harmless.
        con.execute("DELETE FROM nodes WHERE node_id=?", (node_id,))
        con.execute(
            "INSERT INTO removals(node_id, removed_by, at, mode) VALUES(?,?,?,?)",
            (node_id, by, now_iso(), "purge"),
        )
        con.commit()
        return (
            True,
            f"purged {node_id}: node and mailbox records. No history existed. by={by or '?'}",
        )
    con.execute(
        "INSERT INTO removed_nodes(node_id, removed_at) VALUES(?, ?) "
        "ON CONFLICT(node_id) DO UPDATE SET removed_at=excluded.removed_at",
        (node_id, now_iso()),
    )
    con.execute(
        "INSERT INTO removals(node_id, removed_by, at, mode) VALUES(?,?,?,?)",
        (node_id, by, now_iso(), "soft"),
    )
    con.commit()
    return True, (
        f"removed {node_id}: blocked from who, send, register, beat, pull, and notifications. "
        f"History kept (sent {sent} / received {recv}). by={by or '?'}. "
        "Undo with tabm enable --node <actor> --target <disabled-node>."
    )


def bus_restore(con, node_id, by=None):
    """Bring a soft-removed node back, clearing it from removed_nodes and logging
    the action.

    The only path that reverses a removal. A purged node cannot be restored —
    its row in nodes is gone.

    Returns (ok, msg).
    """
    if not node_id:
        return False, "node is required"
    if not is_removed(con, node_id):
        if not con.execute(
            "SELECT 1 FROM nodes WHERE node_id=?", (node_id,)
        ).fetchone():
            return False, f"unregistered node: {node_id} — purged, or never registered"
        return True, f"already active: {node_id} — it was not removed"
    con.execute("DELETE FROM removed_nodes WHERE node_id=?", (node_id,))
    con.execute(
        "INSERT INTO removals(node_id, removed_by, at, mode) VALUES(?,?,?,?)",
        (node_id, by, now_iso(), "restore"),
    )
    con.commit()
    return (
        True,
        f"restored {node_id}: active again across who, send, register, beat, pull, and notifications. by={by or '?'}",
    )


# ═══ tac: named group delivery ═════════════════════════════════════


def tac_action_allowed(con, requester, tac_id, action):
    """🔴 The seam where tac WRITE permissions will attach.

    Identity has landed: every request is signed and the acting node is known, and
    reading a tac is already member-gated at the daemon (/tac_messages refuses a
    non-member). The write actions here — create, add, remove, send, close, link —
    still pass and only audit. Who may perform each is a tac-permission model, a
    deliberate next step this change does not decide; it is not blocked, just not yet
    designed.

    When it lands, changing this one function closes all of them at once: a
    precondition only holds if it sits on the execution path rather than in a
    checklist.

    action is one of 'create', 'add', 'remove', 'send', 'close', 'link'.
    Returns (allowed: bool, reason: str | None).
    """
    return True, None


def _tac_exists(con, tac_id):
    return (
        con.execute("SELECT 1 FROM tacs WHERE tac_id=?", (tac_id,)).fetchone()
        is not None
    )


def bus_tac_create(con, tac_id, label=None, by=None):
    """Create a tac. A tac_id that collides with a node_id is rejected, so a
    send target is never ambiguous."""
    if not tac_id:
        return False, "tac_id is required"
    ok, reason = tac_action_allowed(con, by, tac_id, "create")
    if not ok:
        return False, reason
    if con.execute("SELECT 1 FROM nodes WHERE node_id=?", (tac_id,)).fetchone():
        return (
            False,
            f"name collision: '{tac_id}' is a node name, and a tac cannot share one",
        )
    if _tac_exists(con, tac_id):
        return False, f"tac already exists: {tac_id}"
    con.execute(
        "INSERT INTO tacs(tac_id, label, created_at, created_by) VALUES(?,?,?,?)",
        (tac_id, label, now_iso(), by),
    )
    con.commit()
    return True, f"created tac: {tac_id}" + (f" ({label})" if label else "")


def bus_tac_add(con, tac_id, node_id, by=None):
    """Add a node to a tac. Unregistered or removed nodes are rejected; a duplicate
    membership is harmless because the primary key absorbs it."""
    if not tac_id or not node_id:
        return False, "tac_id and node are required"
    ok, reason = tac_action_allowed(con, by, tac_id, "add")
    if not ok:
        return False, reason
    if not _tac_exists(con, tac_id):
        return False, f"no such tac: {tac_id} (create it first)"
    if not con.execute("SELECT 1 FROM nodes WHERE node_id=?", (node_id,)).fetchone():
        return False, f"unregistered node: {node_id}"
    if is_removed(con, node_id):
        return False, f"a removed node cannot be added: {node_id} (restore it first)"
    con.execute(
        "INSERT INTO tac_members(tac_id, member_node_id, added_at, added_by) "
        "VALUES(?,?,?,?) ON CONFLICT(tac_id, member_node_id) DO NOTHING",
        (tac_id, node_id, now_iso(), by),
    )
    con.commit()
    return True, f"{tac_id} ← {node_id}"


def bus_tac_remove_member(con, tac_id, node_id, by=None):
    """Drop a member from a tac. Not the tac itself. Removing someone who is not a
    member is rejected — a silent success would hide a typo."""
    if not tac_id or not node_id:
        return False, "tac_id and node are required"
    ok, reason = tac_action_allowed(con, by, tac_id, "remove")
    if not ok:
        return False, reason
    cur = con.execute(
        "DELETE FROM tac_members WHERE tac_id=? AND member_node_id=?",
        (tac_id, node_id),
    )
    con.commit()
    if cur.rowcount == 0:
        return False, f"{node_id} is not a member of {tac_id}"
    return True, f"{tac_id} ⊟ {node_id}"


def bus_tac_members(con, tac_id, include_removed=False):
    """Member node_ids for a tac. With include_removed=False, removed nodes are
    excluded, which is what send resolution wants."""
    if include_removed:
        rows = con.execute(
            "SELECT member_node_id FROM tac_members WHERE tac_id=? "
            "ORDER BY member_node_id",
            (tac_id,),
        )
    else:
        rows = con.execute(
            "SELECT member_node_id FROM tac_members WHERE tac_id=? "
            "AND member_node_id NOT IN (SELECT node_id FROM removed_nodes) "
            "ORDER BY member_node_id",
            (tac_id,),
        )
    return [r["member_node_id"] for r in rows]


def bus_tac_mark_read(con, node_id, tac_id):
    """Record that this node has seen this tac.

    Unopened tac deliveries (ACCEPTED or CLAIMED) advance to INJECTED. That is
    the catch-up which lifts the read-before-send refusal, and it runs for
    whichever member asked to view the tac.

    🔴 An observer who is not a member has no deliveries in this tac, so the
       rowcount is zero and nothing changes. Supervision stays read-only.

    Returns the number of deliveries marked.
    """
    cur = con.execute(
        "UPDATE deliveries SET state='INJECTED' "
        "WHERE recipient_id = ? AND state IN ('ACCEPTED','CLAIMED') "
        "AND message_id IN (SELECT id FROM messages WHERE tac_id = ?)",
        (node_id, tac_id),
    )
    con.commit()
    return cur.rowcount


def bus_tac_list(con):
    """Every tac with its member count, excluding removed nodes, and its label."""
    rows = con.execute("""SELECT g.tac_id, g.label, g.created_at, g.created_by,
                  (SELECT COUNT(*) FROM tac_members m
                     WHERE m.tac_id = g.tac_id
                       AND m.member_node_id NOT IN
                           (SELECT node_id FROM removed_nodes)
                  ) AS member_count
             FROM tacs g ORDER BY g.tac_id""")
    return [dict(r) for r in rows]


def bus_tac_messages(con, tac_id, limit=50):
    """A tac's contents, read-only. The messages sent to this tac, newest first.

    🔴 Viewing is not reading. This never touches deliveries, so nothing moves
       to READ.
       Bodies are included: this is the supervision view, which is a different
       act from an agent consuming its own mail.
    """
    # 🔴 The floor on limit belongs here, at the query, because this is the gate
    #    every caller passes through. A negative LIMIT means unlimited in SQLite,
    #    so one leaking through would defeat the ceiling enforced at the boundary
    #    and dump the whole table.
    limit = max(1, int(limit))
    # 🔴 Timestamps here have second precision, so two messages sent in the same
    #    second tie. Breaking the tie by rowid makes "later on top" a contract that
    #    holds regardless of index or query plan. Without it the order follows the
    #    plan and shifts.
    rows = con.execute(
        """SELECT id, sender_id, subject, body, accepted_at, priority
             FROM messages WHERE tac_id=?
             ORDER BY accepted_at DESC, rowid DESC LIMIT ?""",
        (tac_id, limit),
    )
    return [dict(r) for r in rows]


def bus_tac_close(con, tac_id, summary=None, by=None):
    """Close a tac. Closing is final, and a closed tac refuses further sends.

    The summary is fixed at the moment of closing. Re-closing an already closed
    tac is rejected rather than silently succeeding — partly to catch the
    mistake, and partly so a second summary cannot overwrite the first.
    """
    if not tac_id:
        return False, "tac_id is required"
    ok, reason = tac_action_allowed(con, by, tac_id, "close")
    if not ok:
        return False, reason
    # 🔴 Closing is atomic: the UPDATE applies only while closed_at is NULL. Two
    #    concurrent closes on separate connections give the first a rowcount of 1
    #    and the second 0, so a second summary cannot overwrite the first.
    cur = con.execute(
        "UPDATE tacs SET closed_at=?, close_summary=?, closed_by=? "
        "WHERE tac_id=? AND closed_at IS NULL",
        (now_iso(), summary, by, tac_id),
    )
    con.commit()
    if cur.rowcount == 0:
        # A rowcount of 0 means either no such tac or already closed. The response
        # distinguishes them rather than staying silent.
        if not _tac_exists(con, tac_id):
            return False, f"no such tac: {tac_id}"
        return False, f"tac already closed: {tac_id} — it cannot reopen"
    return True, f"closed tac: {tac_id}" + (f" — {summary}" if summary else "")


def bus_tac_link(con, child_tac, parent_tac, by=None):
    """Link tacs: a child continues from a parent.

    A junction table holds them, so direction, forks, and merges all work.
    Parents are usually closed tacs, but that is not enforced — closing and
    linking are separate axes.

    Linking a tac to itself is rejected, both must exist, and a duplicate link
    is harmless because the primary key absorbs it.
    """
    if not child_tac or not parent_tac:
        return False, "both child and parent tac are required"
    if child_tac == parent_tac:
        return False, "a tac cannot link to itself"
    ok, reason = tac_action_allowed(con, by, child_tac, "link")
    if not ok:
        return False, reason
    if not _tac_exists(con, child_tac):
        return False, f"no such tac (child): {child_tac}"
    if not _tac_exists(con, parent_tac):
        return False, f"no such tac (parent): {parent_tac}"
    con.execute(
        "INSERT INTO tac_links(child_tac, parent_tac, linked_at, linked_by) "
        "VALUES(?,?,?,?) ON CONFLICT(child_tac, parent_tac) DO NOTHING",
        (child_tac, parent_tac, now_iso(), by),
    )
    con.commit()
    return True, f"linked: {child_tac} continues from {parent_tac}"


def bus_tac_links(con, tac_id):
    """A tac's links, read-only: parents it continues from, and children that
    continue from it. Direction, forks, and merges are all read out of the
    junction table."""
    parents = [
        r["parent_tac"]
        for r in con.execute(
            "SELECT parent_tac FROM tac_links WHERE child_tac=? ORDER BY linked_at",
            (tac_id,),
        )
    ]
    children = [
        r["child_tac"]
        for r in con.execute(
            "SELECT child_tac FROM tac_links WHERE parent_tac=? ORDER BY linked_at",
            (tac_id,),
        )
    ]
    return {"parents": parents, "children": children}


def cmd_rotate_key(args):
    """Operator-only: replace a node's public key, keeping its history.

    🔴 A local command on purpose — never a bus route. Running it locally is the
    human gate; there is no way to invoke it from a client. Recovery for a lost or
    compromised key, and the only path that reaches a node with history (purge
    refuses those). Prints the outcome and exits non-zero on refusal."""
    con = connect()
    ok, msg = bus_rotate_key(con, args.node, args.pubkey, by=args.by or "operator")
    con.close()
    print(msg)
    sys.exit(0 if ok else 1)


def main():
    p = argparse.ArgumentParser(description="Local key recovery. Use tabc for signed bus requests.")
    sub = p.add_subparsers(dest="cmd", required=True)
    rk = sub.add_parser("rotate-key", help="operator: recover a node key")
    rk.add_argument("node")
    rk.add_argument("pubkey")
    rk.add_argument("--by", default=None)
    args = p.parse_args()
    cmd_rotate_key(args)


if __name__ == "__main__":
    main()
