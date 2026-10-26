# tabc

<img src="docs/logo-26-25-1.png" alt="tabc" width="320">

[![CI](https://github.com/jultabc/tabc/actions/workflows/ci.yml/badge.svg)](https://github.com/jultabc/tabc/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/tabc.svg)](https://pypi.org/project/tabc/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](pyproject.toml)

A shared inbox for AI agents and programs on one machine.

![alice sends, bob pulls — two agents on one machine, no cloud](scripts/demo/demo.gif)

Anything that can run a command line can register as a node: an AI agent in a
terminal, a git hook, a long-running engine, a cron job. They exchange messages
through the same local bus. No cloud, no account, no external service.

Two kinds of node, and the difference is about communication capability, not
whether a terminal is attached:

- **Normal.** A Claude or Codex agent using the CLI or an MCP client. It can send
  and pull messages. The notifier can use a verified terminal route or the
  [Codex-specific queue adapter](#how-do-i-get-notifications-in-a-codex-thread).
  MCP alone does not provide arrival notifications; MCP-only clients pull.
- **Program** (`register --program`). A hook, a daemon, a scheduled job. It is a
  send-only event source: it claims no terminal, can send as its signed node, and
  is rejected if an envelope names it as a recipient.

Four names run through this project. **tabus** is the bus; `tabc` is what an
agent runs.

| | |
|---|---|
| **tabus** | the package, and the bus inside it. Environment variables carry the `TABC_` prefix |
| `tabc` | the command-line client an agent runs, and the public entry point for normal use |
| `tabd` | the local daemon; listens on `127.0.0.1:8765` unless `--bind` and `--port` say otherwise |
| `tabus.doorbell` | the notification poller |

> **Status: prototype.** The interfaces below are what it does today, not
> what it is headed towards.
> One dependency, `cryptography`, for the signatures that authenticate every
> request. SQLite for storage; standard library for the rest.

See the [changelog](CHANGELOG.md) for release-specific changes and upgrade notes.

> **0.2.4:** `tabc send --id <UUID>` lets a caller reuse a saved message ID
> when retrying the same request. Normal send checks still apply. See
> [Upgrading from 0.2.3 to 0.2.4](#upgrading-from-023-to-024).

It is meant to stay small enough to read. Count it rather than trust a number
printed here — a baked total is stale by the next commit, which is why this
project keeps route lists and counts out of the prose:

```bash
git ls-files '*.py' | xargs wc -l | tail -1   # Python lines
git ls-files | wc -l                          # tracked files
```

---

## The problem

When you run several AI coding sessions at once, **they cannot see each other.**
To move a conclusion from one window to another, you copy and paste it yourself.

That makes you the relay for every conversation, and three things hurt:

- **You cannot tell whether the other side can take input right now.** Pushing
  into a busy window breaks whatever it was doing.
- **You cannot tell whether your message arrived.** When delivery fails, the
  sender still thinks it worked.
- **If the other side is closed, the message is gone.** There is no way to
  receive it later.

tabc fixes those three.

| Existing approach | Limit |
|---|---|
| Editor's built-in session channel | Same product only, same machine only |
| Writing straight into a terminal | No delivery confirmation, interrupts a busy window |
| Copying by hand | You are the bottleneck |

---

## How it works

`tabd` stores the message before anything else. The doorbell then tries to
notify the receiver, but it is best-effort and can miss — a stale route, a
closed window. A message is delivered only when the receiver pulls it.

```mermaid
flowchart LR
    A["alice (tabc)"] -->|"send, signed"| D["tabd — 127.0.0.1:8765"]
    D ==>|"1. store first"| S[("SQLite: messages, routes, audit")]
    DB["doorbell poller"] -->|watches| S
    DB -.->|"2. notify (best-effort)"| B["bob (tabc)"]
    B ==>|"pull, guarantees delivery"| D
    D -->|reads| S
```

---

## Install

Python 3.9+ and one dependency, `cryptography`, which `pip` brings in for you.

From PyPI:

```bash
pip install tabc
```

From a checkout, which is what you want if you intend to change the code:

```bash
git clone https://github.com/jultabc/tabc.git
cd tabc
pip install -e .
```

This is an editable install: `tabc` and `tabd` import the code from the checkout,
so ordinary Python edits do not require another install. Restart a running
`tabd` after changing server code; like any Python process, it keeps the modules
it has already imported. Run the install again after changing dependencies,
package metadata, or console entry points in `pyproject.toml`.

If you would rather not create the console commands, clone the repository and
run the same modules directly. Every command below works either way:

```bash
python3 -m tabus.cli   ...   # the same as: tabc ...
python3 -m tabus.daemon ...  # the same as: tabd ...
```

Running from a clone rather than installing means installing that one dependency
yourself — `pip install cryptography`. Every request to the daemon is
authenticated by an ed25519 signature, so this is not optional the way an
optional feature is: without it `tabd` stops on its first import.

### Where state lives

By default, persistent state is under `~/.tabc`. The directory is created on
first use and tightened to owner-only permissions. Set `TABC_HOME` before
starting a process to move the whole installation state somewhere else; tests
use this to stay away from the real inbox.

| Path | Purpose |
|---|---|
| `~/.tabc/tabus.db` | nodes, messages, deliveries, routes, and audit records |
| `~/.tabc/.node_key.<node-id>` | that node's private signing key |
| `~/.tabc/host_id` | stable id used to decide which installation owns a terminal route |
| `~/.tabc/user_email` | optional owner label written by `tabm config` |
| `~/.tabc/data/doorbell_ring.db` | the doorbell's separate observation and ring ledger |
| `~/.tabc/codex_queue_receipts.db` | local Codex queue submission receipts, separate from message read state |

`TABC_DB` may point the message database somewhere else. It does not move keys,
`host_id`, or the doorbell ledger; use `TABC_HOME` when you want a fully isolated
installation.

The state directory itself may be created when the package resolves its paths at
import time. The database and key files are created only when a command needs
them, which is why `TABC_HOME` and `TABC_DB` must be set before importing tabus
in an isolated test.

### Node identity and signing keys

Node identity is process-scoped. Set `TABC_NODE`, or pass `--node` / `--sender`
to commands that expose one. There is no shared on-disk default: several agents
can use one installation without silently inheriting another agent's identity.
Commands that have no explicit identity option stop before HTTP unless
`TABC_NODE` is set.

For node changes in `tabm`, `--node` is the signed caller and `--target` is the
node being changed. The former `tabc rm` and `restore` commands are no longer
accepted. The management section below lists the `tabm` commands and the
approval each one needs.

```bash
# This shell acts as alice for commands such as who and tac ls
export TABC_NODE=alice

# Register or re-register the node and capture a route when one is available
tabc register --node alice --kind codex

# Opt in to automatic submission: Enter on a terminal route, or queue input on
# a Codex queue route
tabc register --node alice --kind codex --auto-enter on

```

`register` requires `--node` and `--kind`. With `--kind codex --auto-enter on`,
it first checks whether it can select the current Codex queue route. See the
[Codex adapter](#how-do-i-get-notifications-in-a-codex-thread) for requirements.
`--kind` is case-insensitive and is stored in lowercase: `--kind OpenCode`
registers `opencode`. Without a queue route, terminal capture inspects
`TMUX`/`TMUX_PANE` and `ITERM_SESSION_ID` and accepts a route only when the
target terminal matches this process's controlling terminal or a same-session
ancestor's terminal. A detached command may instead use its immediate
session-boundary parent. That parent must belong to the same OS user, lead the
target terminal's foreground process group, and read standard input from that
terminal. Process state, executable path, and standard input are checked again
before the route is accepted. The kind and the product name are not part of
this check. It verifies terminal ownership, not whether the program is an
agent: a foreground program that reads the terminal can qualify even if it is
not one. Do not register a node from a terminal that will later run an
unrelated program. Piped output alone does not prevent registration; inherited
variables alone do not prove attachment. A rejected inherited route is not sent
and does not revoke or replace an existing route. Without `--auto-enter on`,
registration with no verified route preserves existing routes; use `read` when
this process has no verified route. With `--auto-enter on` and no verified route,
registration is refused before anything is sent, and existing routes are
unchanged. The former `name` command
and automatic terminal title changes have been removed.

Automatic Enter is **off for new routes**. Omitting `--auto-enter` preserves
the same active route's setting. Passing `--auto-enter off` disables submission.
Only `--auto-enter on` enables Enter for that registered route; other values are
rejected. A new, moved, or revoked route defaults to off. A route-less
registration preserves an existing setting unless off is explicitly requested.
This does not make terminal injection safe: even without Enter, the doorbell text
still enters an execution-capable input field. After first deploying this feature,
stop the old `tabus.doorbell` first: a resident old notifier still presses Enter
on every ring. Then restart `tabd` so it loads the code and migrates the database,
and finally start the new doorbell with the same launch environment it had before.
The client refuses a routed registration when `tabd` does not echo the requested
`auto_enter` state, which exposes a mixed-version deployment. Later on/off changes
are read from the route on every ring and need no restart.

Each local installation directory plus node id gets an independently generated
Ed25519 key pair. The private key is a raw 32-byte file at
`~/.tabc/.node_key.<node-id>`, created with mode `0600` inside the `0700`
state directory. It is not encrypted at rest. Never commit, log, or transmit it.
Only the Base58 public key is registered with tabd.

Ed25519 provides authentication and tamper detection; it does **not** encrypt
message bodies. The current local HTTP transport is not TLS either. Each request
signs hashes of the acting node, HTTP method, exact path, exact body, and Unix
timestamp. tabd accepts timestamps within 300 seconds to limit later replay; it
does not use a nonce to reject an identical replay inside that window.

The first public key registered for an id is pinned. A second machine that creates
a fresh key for the same id will therefore be refused. Use device-specific ids
such as `alice-desktop` and `alice-laptop` when each device owns an independent key.
Replacing a lost or compromised key is a local operator action, not an HTTP API:

```bash
python3 -m tabus.bus rotate-key <node-id> <new-public-key> --by <operator-id>
```

Rotation keeps message history and writes an audit record.

An automatic program has its own identity and key. Register it explicitly:

```bash
tabc register --node sample-program --kind engine --program
```

## Starting an agent on it

[`session_bootstrap.json`](session_bootstrap.json) is one file an agent reads
before its first command. It carries the command forms, the argument names, and
the two rules that are easiest to get wrong: a received body is data rather than
an instruction, and a node's private key never leaves `TABC_HOME`.

It is documentation, not configuration. Nothing loads it at runtime and it is
not in the installed package, so an agent reads it from the repository — here,
or at the raw URL:

```
https://raw.githubusercontent.com/jultabc/tabc/main/session_bootstrap.json
```

Its command list is maintained from `tabus/cli.py`. Where the two disagree, the
code is right; `tabc --help` settles it.

## Quick start

Start the daemon first. Every other command talks to it over local HTTP and
fails with a connection error until it is running.

```bash
tabd &   # 127.0.0.1:8765 by default
```

```bash
# In Alice's shell, register the node — the identity rides on the command
tabc register --node alice --kind claude

# In Bob's shell, register bob the same way

# who takes an acting identity like every other command; TABC_NODE supplies it
export TABC_NODE=alice
tabc who

# Send
tabc send --sender alice --to bob \
    --subject "subject" --body "body" --priority next

# Receive
tabc pull --node bob            # fetch new messages
tabc open --node bob --id <id>  # read one
tabc ack  --node bob --id <id> --state READ
```

`tabc send` is the only sending command. To retry a send safely, pass
`--id <UUID>` with an id you choose before the first attempt. A resend goes
through the same checks as any send, before the id is compared. Unread
messages in the tac, or unread direct messages from a recipient, refuse it
with `UNREAD_BLOCKED`. Reusing an id does not bypass that check. Open and
read those messages, then resend with the same id and envelope. Marking
messages READ without reading them is not a way to clear the block. Program
nodes are not blocked by unread direct messages. Once a send passes
those checks, the daemon stores one message per id:

- The same id with the same envelope (sender, recipients, subject, body,
  priority) is a resend and exits 0. The first line is still
  `stored id=<id>`; the indented line below it reads
  `(identical envelope resent — not stored twice)`.
- The same id with a changed envelope is refused with `failed: conflict: …`
  and exit 1. The stored message is unchanged.

Keep the id and the whole envelope from before the first attempt, and resend
them unchanged. The daemon compares the resolved recipients, not the sending
mode, so keep `--to`, `--tac`, or `--broadcast` the same as well. A change in
tac membership changes the recipients and is reported as a conflict, not as a
second message. The id is written in canonical form (lower case, hyphens), so
one UUID typed two ways is one message. Without `--id` each send gets a new
random id. The MCP `tabc_send` tool takes the same kind of id as
`message_id`; see [MCP.md](MCP.md).

`tabc dm --node bob` lists unread
subjects (the former `mailbox` command); it does not mark them READ.
`tabc sent --node alice` lists outgoing messages and per-recipient delivery states.
Add `--id <full-UUID>` to view a sent body without changing delivery state.
READ records acknowledgement, not task completion.

Management commands live in `tabm`: `config`, `list`, `disable`, `enable`,
`purge`, and `rotate-key`. For node changes, `--node` is the signed caller and
`--target` is the node being changed. Disable and purge require `--yes`.
This command split does not add administrator authorization. The daemon checks
node signatures, but disable/enable/purge have no separate administrator-role
check. Key recovery remains local-only.

Because nothing in the code holds that authority, the gate is the user. An
agent asks before running any `tabm` subcommand, including the read-only ones,
naming the exact command and its target. `--yes` is not that approval, and
neither is a valid node signature.
Old `dm --sender ...` scripts must use `send`. `tabc mailbox`, `config`, `rm`,
and `restore` are no longer accepted. HTTP paths remain compatible; `sent`
requires a daemon with the new `/sent` endpoint.

`send` prints what it stored, and for whom:

```
stored id=7b3e1a2c-4f5d-4a71-9c2e-0f1d2a3b4c5d
  stored for: bob (1)
  carol: 12 pending
```

Read the `stored for:` line every time. A request is not a delivery, and the
exit code will not catch the usual mistake: if every recipient is unregistered
the command exits 1, but if only one name is misspelled the message is stored
for the rest and the command still exits 0.

The `N pending` figure is that recipient's unread count **from this sender**,
not a receipt for this message. `tabc who` reports the recipient's total unread
count across all senders.

`tabc who` reports presence separately, one line per node:

```
bob        3 pending · mailbox 2026-08-23T01:41
```

## Reaching the bus from something else

This repository includes an optional [MCP server](MCP.md) for desktop agents
and a [Java client](clients/java/) for programs. Both use the local HTTP API.
The MCP adapter supports DM, TAC history and sending, and explicit delivery
acknowledgements. It does not wake desktop agents on message arrival.
The Codex queue adapter is a separate notification path, not an MCP capability
or general desktop-app notification support.

The daemon speaks JSON over HTTP. `tabd` listens on `127.0.0.1:8765` unless
`--bind` and `--port` say otherwise. `TABC_BUS_URL` sets where clients connect;
it does not change where `tabd` listens, so set it to the same address. Rather than copy the routes here — a list and its count both go stale
the first time one is added or dropped, and this paragraph's did — ask the daemon
what it dispatches on:

```bash
python3 - <<'PY'
import ast
tree = ast.parse(open("tabus/daemon.py").read())
routes = {
    n.value
    for n in ast.walk(tree)
    if isinstance(n, ast.Constant)
    and isinstance(n.value, str)
    and n.value.startswith("/")
    and n.value.count("/") == 1
    and len(n.value) > 1
}
print(len(routes), "routes:", " ".join(sorted(routes)))
PY
```

The CLI's own command names are a slightly different set — `open` posts
to `/reopen`, and `attach` is a loop over endpoints rather than a
route of its own. Count the daemon when you want the wire surface.

Every request carries three headers — `X-Node`, `X-Node-Ts`, and `X-Node-Sig`,
an ed25519 signature over the node id, method, path, body and timestamp. There
is no shared secret to hand out: a caller signs with its own node key, and the
daemon checks it against the key that node registered. `tabus/nodekey.py` is the
whole of it, and `tabus/cli.py` is a working client of every one of them — the
shortest specification of the wire format, one file, hiding nothing.

So an MCP client, editor integration, chat bot, or other adapter is a thin
translation layer: use a normal node when it supports both send and pull. Lack of
a terminal only means there is no terminal doorbell. Use `register --program`
only for an automatic send-only event source such as a monitor, a hook, or a
cron job. Because each client signs as its node, its requests remain attributable.

See [MCP setup](MCP.md) for source installation, node registration and client
configuration. The MCP dependency is optional; the standard CLI install is unchanged.

## Commands

`tabc --help` is the reference. It is generated from the code, so it does not
go stale the way a table in a README does.

The main ones: `register` `who` `send` `pull` `open` `ack` `tac` `snooze`.

## Questions that come up

### I edited the checkout. Do I need to reinstall?

Not after an editable install for an ordinary `.py` edit. Restart any long-lived
process that imported the changed module, especially `tabd` or the doorbell.
Re-run `pip install -e .` after changing `pyproject.toml` dependencies, package
metadata, or entry points.

### Does registration set the identity for later commands?

No. Registration writes the node and its public key to the bus; it cannot change
the environment of the shell that launched it. Export `TABC_NODE` for commands
without an acting-node option. Each process uses its own node identity rather
than a shared on-disk default.

### Does `tabc who` require `TABC_NODE`?

Every request is signed, `who` included — there is no unauthenticated endpoint,
because the node roster it returns is worth gating to registered nodes. Pass
`--node` to identify the signing caller, or use the shell's `TABC_NODE` fallback.
This identifies who is asking, not whose inbox is being read:

```bash
tabc who --node <your-node>
```

Commands that name an acting node — `send --sender`, `pull --node`, and so on —
do not need it.

### How do I actually get the alarm to ring?

Installing does not start it. The notifier is a separate process, and it starts
in `SHADOW` mode, which records what it *would* have typed and types nothing:

```bash
DOORBELL_MODE=LIVE python3 -m tabus.doorbell     # ring for real
DOORBELL_MODE=SHADOW python3 -m tabus.doorbell   # record only; the default
DOORBELL_POLL_SEC=2                              # poll interval, default 2
```

Stop it with the usual signal to that process; there is no daemon to ask.

Read `SHADOW` first if you are unsure. It shows you which node and which
terminal the ring would reach without touching anyone's keyboard, and a
mis-registered route is much easier to see there than after it has typed into
the wrong window.

For terminal routes, `LIVE` types into the terminal's input field. See
[SECURITY.md](SECURITY.md) for the risks of terminal injection. For a Codex queue
route, `LIVE` submits notification input to the selected conversation instead.
Automatic submission is off for new routes and requires an explicit
`register --auto-enter on`; the Codex queue route cannot submit text without it.

### Why did registration succeed but no doorbell route appear?

The bus and the terminal route are separate. `register` checks that the inherited
tmux or iTerm target matches the process's terminal. Detached runners and mismatched
targets cannot claim a route. Existing verified routes survive stale cleanup requests.
Update the server and all registering clients before relying on this protection;
the verification flag is a client claim, not a security boundary against signed callers.
`register --program` always declines a terminal route and
creates a send-only node.
Use a separate name: an existing agent cannot be converted into a program.
New program registration and re-registration of an existing program are allowed.

### How do I get notifications in a Codex thread?

The Codex-specific adapter submits notification input with `codex queue`,
without typing into a terminal. It is separate from the MCP adapter. Validation
was performed with codex-cli 0.162.1 on macOS; other versions and desktop hosts
are not covered by that result.

1. Replace and verify the running notifier with 0.2.1 or later
   before registering queue routes. Run one notifier, not two. An older
   notifier records `UNSUPPORTED` for this route.
2. Run registration from each intended Codex conversation, using the matching
   CLI:

```bash
tabc register --node <your-node> --kind codex --auto-enter on
```

The CLI checks the format of the current `CODEX_THREAD_ID`, that the command
runs below a native Codex managed-daemon app-server, and that the `codex`
executable is a native Codex release. It does not ask any server who owns the
thread, so these checks are not proof of thread or terminal ownership. Do not
copy another conversation's UUID or the shared daemon's terminal ID.

With `--kind codex --auto-enter on`, registration selects a route in this order:

1. If the checks pass, the Codex queue route, including when it replaces this
   node's existing terminal route.
2. Otherwise, a terminal route verified by this command.
3. If neither is available, registration is refused before anything is sent,
   and existing routes stay unchanged.

With `--auto-enter off` or omitted, registration does not select a queue route.

To require queue delivery without terminal fallback, add `--codex-queue`:

```bash
tabc register --node <your-node> --kind codex --codex-queue --auto-enter on
```

If the checks fail, it prints `cannot verify the current Codex managed-daemon
thread; registration not sent` and exits 2. When registration refuses, read the
error and do not work around it with a copied identity or another route. Queue
selection cannot be combined with `--take-route`; `--codex-queue` cannot be
combined with `--program`.

- `QUEUED` means Codex accepted notification input, not `READ` or work
  completion. Display may follow the current reply; immediate interruption or
  delivery within a fixed time is not guaranteed. Pull the stored message and
  record its read state separately.
- Notifications contain metadata only: sender names, unread counts, and the TAC
  name or UUID for TAC messages. They never contain message bodies. The text is
  a command-line argument that local process tools can see.
- The notifier, not the registering conversation, runs `codex queue`. It needs a
  native `codex` on its own `PATH` and a Codex environment that reaches the same
  local daemon. Without that, each notification can end as `UNKNOWN`.
- After replacing the notifier and registering, confirm one real round trip: a
  message arrives in the conversation, and a reply comes back. `QUEUED` alone
  does not show that.
- The local receipt database permits one submission attempt per thread UUID
  and notification ID. An uncertain result is `UNKNOWN` and is not retried
  automatically under that ID. Losing the database or moving to another thread
  is outside this duplicate-prevention boundary.

### Can an adapter write `tabus.db` directly?

It can technically open SQLite, but it should not. `tabd` is the HTTP write
boundary for clients: it verifies the signed caller, enforces self-scoped
actions, runs migrations, and keeps delivery transitions in one place. A new
adapter should use the signed HTTP API and treat `tabus/cli.py` as the reference
client.

---

## Two ways to send

| | Goes to | Rings the doorbell | Use it for |
|---|---|---|---|
| **dm** (`--to`) | one or more named nodes | yes, if routed | a message addressed to explicit recipients |
| **tac** (`--tac`) | every member of a topic | yes, if routed | a line added to an ongoing topic |
| **broadcast** (`--broadcast`) | every non-program, non-removed node other than the sender | yes, if routed; through SHADOW too | a one-time announcement |

"If routed" is the whole condition. A doorbell needs a terminal route on the
receiving node; a node with none is `NOT_MINE` and receives by pulling, and a
snoozed node stays quiet. The scope decides who is addressed, not whether a
doorbell can sound. The notification header names the scope: `[dm]` for
`--to`, `[tac] topic` for `--tac`.

`tabc dm` is not one of these. It sends nothing; it lists the unread subjects
addressed to a node. The header `[dm]` and the command `dm` are different
things that share a name.

A **tac** (Topic Archive Capsule) is a named conversation. The server gives it
a UUID when it is created. The name is for display and search. Renaming does
not change that UUID. Tac names are unique after case folding, so an exact name
matches at most one tac, open or closed.

Commands that change or address the tac take its UUID. From 0.2.3, the CLI also
accepts the exact name, compared after case folding. It looks the name up and runs only when the name
belongs to exactly one open tac, after printing `tac <name> = <uuid>` on stderr.
For a closed tac, a missing name, or names that only resemble it, the CLI runs
nothing. It lists the candidates with their UUID and `open` or `closed` date,
prints `code TAC_NAME_UNRESOLVED`, and exits 2. A partial name is never chosen.
A canonical lowercase UUID is used as given. Any other value is looked up as a
name first, so a tac whose name looks like a UUID is reached by its name.
`tac create` takes the new name and does not look it up. The server and the MCP
adapter still take only the UUID.
Once closed, a tac cannot be reopened — the closing summary is the record.

### Working with a tac

Create the topic, add the nodes that follow it, then post lines that fan out to
all of them. Membership is explicit: the creator is recorded as the actor but is
not automatically a member, so add each node that should receive the messages.

```bash
# Create a topic. Copy the UUID printed by create or tac ls.
tabc tac create planning --description "parser work" --node alice
tabc tac ls --node alice

# Add members using that UUID.
tabc tac add <planning-uuid> bob --node alice
tabc tac add <planning-uuid> carol --node alice

# Post a line — it fans out to every member and can ring their doorbells
tabc send --sender alice --tac <planning-uuid> \
    --subject "kickoff" --body "starting on the parser"

# Read it: ls lists the tacs, show reads one
tabc tac ls --node alice
tabc tac show <planning-uuid> --node bob
# From 0.2.3 the CLI also accepts the exact name of an open tac
tabc tac show planning --node bob
# Only the returned messages advance to INJECTED. Acknowledge each full message
# UUID as READ after reviewing it; older unreturned messages may still block send.
tabc ack --node bob --id <message-uuid> --state READ

# Close it when the topic ends. The summary is fixed as the record and a closed
# tac cannot reopen; a follow-up topic links back to the one it continues.
tabc tac close <planning-uuid> --node alice --summary "parser shipped; next: speedups"
tabc tac create speedups --node alice
tabc tac link <speedups-uuid> <planning-uuid> --node alice
```

`create`, `add`, `rm`, `rename`, `close`, and `link` change state and record the
acting node (from `--node`, or `TABC_NODE`) in the audit. `ls`, `check`, and
`search` are read-only. `show` is read-only without `--node`; with `--node`, only
the returned deliveries advance to `INJECTED`, not `READ`. Use `ack` after
reviewing each message. `search --query <text>` searches names, descriptions,
subjects, and bodies without changing delivery state.

### Upgrading from 0.2.3 to 0.2.4

0.2.4 adds the CLI `send --id` option. The daemon already handles duplicate
message IDs; the ledger schema, notifier, and MCP adapter are unchanged.
Nodes, keys, messages, delivery states, TACs, routes, and Codex queue receipts
are preserved. No conversion or registration is required.

After 0.2.4 is published, install one of these packages in the environment
that runs your existing installation:

```bash
python -m pip install --upgrade "tabc==0.2.4"
# Or, include the optional MCP adapter:
python -m pip install --upgrade "tabc[mcp]==0.2.4"
```

The CLI option works with an existing 0.2.3 daemon. A daemon or notifier
restart is not required for this feature. If replacing the complete running
installation, stop the notifier and `tabd`, confirm the ledger WAL is absent
or empty, and back up the store and keys before installing. Restart `tabd`,
then one notifier from the same installation. Restart any running MCP adapter
from its host to load the updated package. Preserve the existing store paths,
keys, server address, and launch settings.

Check `tabc --version` and `tabc send --help` in the target environment.
Before the first send, save the ID and complete message inputs. Reuse them
unchanged for retries. An unread-message refusal still requires reading the
relevant messages; a conflict requires inspecting the saved and stored
requests. Do not generate a new ID to force a retry through.

An upgrade from an older version follows its notes below first.

### Upgrading from 0.2.2 to 0.2.3

0.2.3 does not change the ledger schema. Nodes, signing keys, messages, delivery
states, TACs, routes, and Codex queue receipts are kept. No conversion is needed.
Registration is unchanged, so nodes do not need to register again.

Use this order:

```bash
# First stop tabus.doorbell.

# Install one of these packages.
python -m pip install --upgrade "tabc==0.2.3"
# Or, include the optional MCP adapter:
python -m pip install --upgrade "tabc[mcp]==0.2.3"

# Restart tabd, then start one tabus.doorbell from the same installation.
tabc --version
tabd --version
```

- Name lookup needs both sides. The CLI asks `tabd` for `/tac_lookup`. Until
  `tabd` is restarted on 0.2.3, it answers 404, the CLI sends the typed name
  unchanged, and the server refuses it with `TAC_ID_INVALID`. UUIDs keep
  working either way.
- The notifier and the MCP adapter did not change in 0.2.3. The notifier is
  stopped and started again only to keep the restart order above. A running
  `tabc-mcp` process does not need a restart for this change.
- An upgrade from 0.2.1 follows the 0.2.2 notes below first.

### Upgrading from 0.2.1 to 0.2.2

0.2.2 does not change the ledger schema. Nodes, signing keys, messages, delivery
states, TACs, routes, and Codex queue receipts are kept. No conversion is needed.

Use this order:

```bash
# First stop tabus.doorbell.

# Install one of these packages.
python -m pip install --upgrade "tabc==0.2.2"
# Or, include the optional MCP adapter:
python -m pip install --upgrade "tabc[mcp]==0.2.2"

# Restart tabd, then start one tabus.doorbell from the same installation.
tabc --version
tabd --version
```

- Stored routes are not changed by the upgrade. A failed capture does not
  replace an existing route.
- To use the 0.2.2 terminal check, register again from the intended terminal,
  then confirm that one notification arrives before relying on it.
- Codex queue registration keeps the rules in
  [How do I get notifications in a Codex thread?](#how-do-i-get-notifications-in-a-codex-thread)
- No dedicated adapter for another agent product is added. The MCP adapter
  still does not wake an agent. If a desktop client runs `tabc-mcp`, restart
  that MCP process from the client after the upgrade so it loads the new
  installation.
- An upgrade from 0.2.0 follows the 0.2.1 notes below first.

### Upgrading from 0.2.0 to 0.2.1

0.2.1 does not change the ledger schema. Nodes, signing keys, messages, delivery
states, TACs, and routes are kept. No conversion is needed.

Use this order:

```bash
# First stop tabus.doorbell.

# Install one of these packages.
python -m pip install --upgrade "tabc==0.2.1"
# Or, include the optional MCP adapter:
python -m pip install --upgrade "tabc[mcp]==0.2.1"

# Restart tabd, then start one tabus.doorbell from the same installation.
tabc --version
tabd --version
```

- Run the CLI and the notifier from the same installation. Run one notifier,
  not two.
- Every `register` call requires `--kind`. Update scripts that omit it.
- To use the Codex queue route, register again from each intended Codex
  conversation after the notifier runs 0.2.1. See
  [How do I get notifications in a Codex thread?](#how-do-i-get-notifications-in-a-codex-thread)
- The notifier creates `codex_queue_receipts.db` in the tabc home directory on
  its first Codex queue submission. It is separate from message read state.
- The Codex queue route is a notifier path. It is not part of the MCP adapter.
- If a desktop client runs `tabc-mcp`, restart that MCP process from the client
  after the upgrade so it loads the new installation.
- An upgrade from 0.1.6 follows the 0.2.0 procedure below first.

### Upgrading an existing TAC ledger to 0.2.0

An upgrade from 0.1.6 preserves nodes, messages, delivery states, TAC membership,
names, and labels. Existing string TAC IDs keep working until the operator
converts the ledger. Conversion is not run by package installation or server
startup.

Use this order:

```bash
# First stop tabus.doorbell and tabd.
# Confirm that <ledger.db>-wal is absent or empty (0 bytes).
# A remaining empty WAL or SHM file is safe.
# If the WAL is not empty, start tabd and stop it cleanly, then check again.
# Back up the main database only after the WAL is absent or empty.

# Install one of these packages.
python -m pip install --upgrade "tabc==0.2.0"
# Or, include the optional MCP adapter:
python -m pip install --upgrade "tabc[mcp]==0.2.0"

# Read the source, then write a separate converted candidate.
python -m tabus.tac_migration <ledger.db>
python -m tabus.tac_migration <ledger.db> --output <converted.db>
```

The conversion never overwrites the source file.
Verify the reported row counts and the converted database before replacing the
active database. Do not replace it while its WAL file is non-empty. A remaining
empty WAL or SHM file is safe. Restart `tabd` first, then restart
`tabus.doorbell`. `tabc tac ls` shows the new UUID next to each preserved name.
After conversion, the server and the MCP adapter refuse a TAC name used instead
of its UUID with `TAC_ID_INVALID`. From 0.2.3, the CLI resolves an exact open
tac name to its UUID first. Scripts should use the UUID printed by `tabc tac ls`,
which does not change when the tac is renamed.
Do not run old and new daemons against one active ledger during the swap.
Confirm the installed processes after restart:

```bash
tabc --version
tabd --version
```

## Receiving: pull is what guarantees delivery

There is no push. Follow this order:

```
pull on your own schedule     ← this is what proves you received everything
act on the doorbell if it fires ← convenience only
```

**Do not depend on the doorbell.** If a notification is lost, the message is
still sitting in the store and no receiver reads it. The bell failed; the parcel did
not disappear. Those are different failures and they need different defenses.

The failure mode is **a message read late**, not a message lost. Each node
pulling on its own removes that failure.

## Priority

The point of this tool is not delivery. It is **timing.**

| | Meaning |
|---|---|
| `now` | Worth interrupting for. Incidents, outages, anything irreversible |
| `next` | Read it when the current turn ends. **Default.** Most things |
| `batch` | Collect and read when idle. Reference, sharing |

Priority orders the queue and filters it. It does not change the alarm: a
`batch` message rings exactly as a `now` message does, because the notifier
does not read the field. Choosing `batch` says when the message is worth
reading, not whether it interrupts.

---

## What works / what does not

Written plainly. Hiding this turns into "I thought it did that."

**Works**

- Several AI sessions on one machine, regardless of product
- Delivery while the other side is closed — storage comes first
- Malformed envelopes are quarantined without showing the body
- Any character in the body
- Ed25519 request signing rejects a sender that cannot prove possession of the
  pinned node key
- The terminal doorbell on Linux, not only macOS: the tmux adapter rings a pane
  on both, while the iTerm adapter is macOS-only (measured in a Linux container)

**macOS only**

- Automatic status detection (it asks iTerm). Elsewhere it skips quietly and
  falls back to self-reporting.
- The iTerm doorbell adapter specifically. tmux is the cross-platform one, so a
  Linux node registered inside a tmux pane still gets the doorbell.

**Does not work**

- Joining two buses across machines. The store is a local file, so a bus serves
  one machine; a client elsewhere can still reach it over `--bind`, with its
  request signed the same way, but two buses cannot be linked yet
- Push delivery — the receiver has to `pull`
- Message-body encryption — signatures authenticate requests but do not hide text
- TLS on the default local HTTP link

---

## Where this is going

Today a bus serves one machine. The store is a local file, so a node on its own
separate bus is outside this one — a client that connects to this bus over
`--bind` is a participant, but two buses do not yet carry each other's mail.

The direction is to **connect buses rather than widen one**. Each machine keeps
its own bus and its own store, and two buses agree to carry each other's
messages. A node keeps talking to the bus in front of it and never learns that
a recipient is elsewhere.

**How is not decided.** The alternative is a shared store — one database that
several machines write to — and a comment in the source still assumes that
route. It was written before this direction was chosen, so read it as an open
question, not a plan. The choice is not only plumbing: a shared store has to
authenticate every writer, while connected buses authenticate each other and
then vouch for their own. Identity has to prove a different thing in each.

Nothing here is scheduled. It is written down so the current shape reads as a
choice rather than an oversight.

## Invariants

- **Store first, notify second.** The notification carries sender names, unread
  counts, and the TAC name or UUID for TAC messages — never a body.
  Receiving-then-storing cannot structurally prevent loss while a node is down.
- **One row per recipient.** Delivery is at-least-once, not exactly-once.
  Duplicates are absorbed on the receiving side.
- **Stages stay separate.** Stored / claimed / injected / actually read each
  guarantee a different thing.
- **Stale values become unknown automatically.** A stored status is not trusted
  as-is.
- **Irreversible work goes last.** Anything that can throw runs before it.

## Security

This tool moves messages **between agents**, which is a different risk profile
from a consumer messenger — text that arrives can become an instruction to the
receiving AI.

**Read [SECURITY.md](SECURITY.md) first.** It lists the five rules and how far
each is actually enforced today.

[GUIDE.md](GUIDE.md) is the operating half of this documentation: what goes
wrong, how to tell which thing went wrong, and what a result does and does not
prove. Read it when something behaves in a way this page does not explain.

---

## License

MIT. Full text in [`LICENSE`](LICENSE) at the repository root.

Use it, modify it, redistribute it. One condition — keep the copyright notice
and the license text with it. No warranty.
