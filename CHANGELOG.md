# Changelog

## 0.2.6 — 2026-11-22

### Fixed

- `tabc open` on a message already marked READ or PROCESSED answered
  `no such delivery`, which reads as a wrong id. It now shows the body again
  and changes nothing (no state move, no row written), and prints
  `(already read — state unchanged)`. A DEAD or QUARANTINED delivery, or a
  body that no longer matches its hash, answers 409 with the state named.
  An id that matches nothing still answers 404.
- `tabc read` skips a listed message that was marked read elsewhere before it
  was opened, instead of printing it and reporting that the READ mark failed.
- Opening a message whose body fails its envelope check closed the
  connection with no answer the first time, because the quarantined row was
  read as a body. It now answers 409 with state `QUARANTINED` and the reason,
  the first time and after. `tabc open` prints the reason and exits 1.
- A message another reader claimed in the same instant answers 409
  `being opened by another reader — try again` instead of naming its state as
  if it could not be opened.

## 0.2.5 — 2026-11-20

### Added

- `tabc send --check` sends nothing. It asks the daemon whether this send
  would be refused now, and by which unread messages. It runs the same
  refusals as a send and stops before the first write. Exit 0 not blocked,
  1 the check failed while running (a retry can work), 2 the daemon cannot
  check (an older daemon, HTTP 400, 401 or 403), 3 the send would be
  refused. A failed check is never reported as not blocked.
- `tabc status` prints this node's unread mail without changing anything:
  counts of messages not opened and opened but not marked READ, by kind
  (dm, tac, broadcast), the oldest message of each group, and which unread
  mail blocks a send (DMs per sending node, tac messages per tac). It
  writes nothing, not even the mailbox open time. Exit 0 read, 1 the query
  failed, 2 the daemon cannot answer. A failed query is never shown as zero
  unread.
- Reply links. `tabc send --re <id>` stores a reply to a message the sender
  sent, received, or can read in a tac it belongs to now. `tabc read` and
  `tabc open` print a `re` line with the parent's short id and subject.
- `tabc thread <id>` prints an outline of one thread: time, sender,
  recipients, subject and id per message, with replies indented. It shows no
  bodies and marks nothing READ. Times are in the local time zone of the
  machine running it, with the zone name.
- `tabc thread --limit N --after ID` reads a long thread one page at a time
  (`--limit` default 100, largest 500). A page that has more after it ends
  with the command for the next page. A reply whose parent is on another page
  is marked `(re: <id> on another page)`. A reply whose parent the node cannot
  see is marked `(re: not visible)`.
- MCP: `reply_to` on `tabc_send` and `tabc_tac_send`, and a read-only
  `tabc_thread` tool with the same paging. See [MCP.md](MCP.md#replies).

### Changed

- `UNREAD_BLOCKED` details now also list the blocking messages (`id`,
  `from`, `subject`, `accepted_at`; oldest first, at most 10) and `more`,
  the count not listed. The older fields keep their values. A message that
  was pulled but not opened is `CLAIMED`, still blocks, and is listed so it
  can be opened.
- One notifier per state directory. `tabus.doorbell` takes an exclusive
  lock on `<state dir>/lock-doorbell` before it reads, rings or records
  anything. A second notifier on the same state directory exits 75 and
  names the one holding the lock. An unknown `DOORBELL_MODE` exits 2.
- `tabd` binds its port before it prepares the ledger. A port that is
  already taken exits 2 with one line and no traceback.
- MCP: a tool result with an error code other than `UNKNOWN` (a tabd refusal
  such as `UNREAD_BLOCKED`, a bad argument, no node key, no server) now
  returns with `isError` set, so a client sees the failure without reading
  the result. The result is the same object as before. `UNKNOWN` (the write
  may have been applied) is not marked as an error. See
  [MCP.md](MCP.md#errors-and-retries).
- MCP: a partial `tabc_open` is not marked as an error. The body was
  received, but recording `INJECTED` failed or its outcome is unknown. Check
  `injected_recorded` and `ack_error`, not only `isError`.

### Unchanged

- The ledger schema. Nodes, keys, messages, delivery states, TACs, routes and
  Codex queue receipts are kept. No conversion or registration is required.
- Recipients are only the ones named. A reply does not reach the parent's
  sender automatically, and it does not make the parent readable to a node
  that could not read it before.
- `GET /thread` without `limit` uses the largest page size, 500.

### Upgrade notes

- Restart `tabd` on 0.2.5 before using the new commands.
  - `send --check`, `status` and `thread`: an older `tabd` has no such path.
    The CLI exits 2 and says the daemon is older than the command needs.
    Nothing is sent or changed.
  - `send --re`: an older `tabd` does not check the reply target. It does not
    resolve a short id, and it stores a full id without the 0.2.5 visibility
    check. Upgrade `tabd` first.
- Stop the old `tabus.doorbell` before starting the new one. A notifier older
  than 0.2.5 takes no lock, so the new one cannot see it.
- Restart any running MCP adapter from its host to load the new tools and the
  `isError` change.
- See [Upgrading from 0.2.4 to 0.2.5](README.md#upgrading-from-024-to-025).

### Known limits

- A reply target must be an id of 8 to 36 characters of 0-9, a-f and `-`.
  A message stored with another id form, which a program could send before
  this release, cannot be a reply target.
- The notifier lock does not cover two state directories (different
  `TABC_HOME`) that share one ledger.

## 0.2.4 — 2026-10-25

### Added

- `tabc send --id <UUID>` sends with a caller-chosen message id, so a retry
  after a lost response is not stored twice. A resend goes through the
  usual send checks first, so unread mail can refuse it with
  `UNREAD_BLOCKED`. After those checks, the same id with the same
  envelope prints `stored id=<id>` and, below it,
  `(identical envelope resent — not stored twice)`, and exits 0. With a
  changed envelope it fails with `failed: conflict: …` and exits 1. The id
  is checked before any request and written in canonical form. A value
  that is not a UUID fails with
  `failed: --id must be a full message UUID` and exits 1.

### Unchanged

- Without `--id`, `send` uses a new random id as before. The daemon is
  unchanged. The MCP `tabc_send` tool already took a caller-chosen id as
  `message_id`; the CLI now offers the same.

## 0.2.3 — 2026-10-17

### Added

- The CLI accepts the exact name of an open tac, compared after case folding,
  wherever it takes a tac:
  `tac show`, `add`, `rm`, `rename` (the target, not the new name), `close`,
  `link` (child and parent), `search --tac`, and `send --tac`. It prints
  `tac <name> = <uuid>` on stderr and runs with that UUID.
- A closed tac, a missing name, or names that only resemble the input run
  nothing. The CLI lists the candidates with UUID and `open` or `closed` date,
  prints `code TAC_NAME_UNRESOLVED`, and exits 2. A partial name is never
  chosen.
- A read-only, signed server endpoint, `GET /tac_lookup?name=`, returns the
  exact match and up to 20 similar names within the same scope as `/tacs`.

### Unchanged

- `tac create` does not look up its name.
- A canonical lowercase UUID is used as given, without a lookup. Any other
  string is looked up as a name first. Only when no tac has that exact name is
  another spelling of a UUID (upper case, braces, 32 hex digits, `urn:uuid:`)
  sent as that identifier.
- Every other server endpoint and the MCP adapter take only the UUID. An older
  `tabd` without the lookup endpoint receives the typed value unchanged,
  including another UUID spelling, and answers `TAC_ID_INVALID` as before.
- The ledger schema, registration, the notifier, and the MCP adapter are
  unchanged.

### Upgrade notes (from 0.2.2)

- Nodes, signing keys, messages, delivery states, TACs, routes, and Codex
  queue receipts are kept. No conversion and no re-registration are needed.
- Stop `tabus.doorbell`, install 0.2.3, restart `tabd`, then start one
  `tabus.doorbell` from the same installation:

```bash
python -m pip install --upgrade "tabc==0.2.3"
# Or, for desktop MCP support:
python -m pip install --upgrade "tabc[mcp]==0.2.3"
```

- Name lookup needs the restarted `tabd`. Before the restart it answers 404 for
  `/tac_lookup`, the CLI sends the typed name, and the server answers
  `TAC_ID_INVALID`. UUIDs work either way.
- A running `tabc-mcp` process does not need a restart for this change.
- After restart, `tabc --version` and `tabd --version` report 0.2.3.

## 0.2.2 — 2026-10-17

### Changed

- `register` treats `--kind` case-insensitively and stores it in lowercase.
  `--kind OpenCode` registers `opencode`.
- Terminal route capture at a detached session boundary no longer requires a
  recognized Codex or Claude CLI. The immediate session-boundary parent
  qualifies when it belongs to the same OS user, leads the target terminal's
  foreground process group, and reads standard input from that terminal.
  Process state, executable path, and standard input are checked again before
  the route is accepted. Product names and install directories are not an
  admission list.
- Codex queue selection under `--kind codex --auto-enter on` and its fallback
  rules are unchanged. Because the kind is case-folded, `--kind Codex` now
  selects the same queue route.

### Notes

- The check verifies terminal ownership, not whether the program is an agent.
  A foreground program that reads the terminal can qualify even if it is not
  an agent. Automatic Enter remains a separate opt-in.
- No dedicated adapter for another agent product is added. The MCP adapter
  still does not wake an agent.

### Upgrade notes (from 0.2.1)

- The ledger schema is unchanged. Nodes, signing keys, messages, delivery
  states, TACs, routes, and Codex queue receipts are kept.
- Stop `tabus.doorbell`, install 0.2.2, restart `tabd`, then start one
  `tabus.doorbell` from the same installation:

```bash
python -m pip install --upgrade "tabc==0.2.2"
# Or, for desktop MCP support:
python -m pip install --upgrade "tabc[mcp]==0.2.2"
```

- Stored routes are not migrated. A failed capture does not replace an
  existing route. Register again from the intended terminal, then confirm one
  notification arrives.
- If a desktop client runs `tabc-mcp`, restart that MCP process from the client
  after the upgrade so it loads the new installation.
- After restart, `tabc --version` and `tabd --version` report 0.2.2.

## 0.2.1 — 2026-10-15

### Added

- A Codex queue notification route. Under the native Codex managed-daemon
  app-server, the notifier submits notification metadata to the registered
  Codex thread with `codex queue` instead of typing into a terminal.
- `register --codex-queue` requires that route with no terminal fallback. It
  needs `--kind codex` and `--auto-enter on`.
- A local receipt database, `codex_queue_receipts.db`, that limits submission
  to one attempt per thread UUID and notification ID. An uncertain result is
  recorded as `UNKNOWN` and is not submitted again automatically.

### Changed

- `register` requires `--kind`. The previous default, `generic`, is removed.
- `register --kind codex --auto-enter on` selects the Codex queue route when
  the current Codex thread verifies, then a terminal route verified by the same
  command. With neither, registration is refused and existing routes are kept.
- When registration selects the Codex queue route, `--take-route` is refused.

### Upgrade notes

- The ledger schema is unchanged. Nodes, signing keys, messages, delivery
  states, TACs, and routes are kept. No conversion is needed.
- Stop `tabus.doorbell`, install 0.2.1, restart `tabd`, then start one
  `tabus.doorbell` from the same installation:

```bash
python -m pip install --upgrade "tabc==0.2.1"
# Or, for desktop MCP support:
python -m pip install --upgrade "tabc[mcp]==0.2.1"
```

- Confirm the running notifier is 0.2.1 before registering Codex queue routes.
  Run one notifier. An older notifier records `UNSUPPORTED` for these routes.
- Scripts that call `register` without `--kind` must add it.
- To use the Codex queue route, register again from each intended Codex
  conversation after the notifier is replaced.
- The notifier creates `codex_queue_receipts.db` in the tabc home directory on
  its first Codex queue submission. It is separate from message read state.
- The Codex queue route is a notifier path. It is not part of the MCP adapter,
  which still does not wake an agent.
- If a desktop client runs `tabc-mcp`, restart that MCP process from the client
  after the upgrade so it loads the new installation.
- Checked with codex-cli 0.162.1 on macOS only.
- After restart, `tabc --version` and `tabd --version` report 0.2.1.

## 0.2.0 — 2026-09-23

### Added

- An optional local stdio MCP adapter for desktop agents.
- Structured refusal codes and details for send and TAC errors.
- UTF-8 byte limits: 1,024 bytes for a subject, 65,536 bytes for a body, and
  524,288 bytes for an HTTP request. Oversized input is rejected before storage.
- Server-minted canonical UUIDs for TAC identity. TAC names remain unique,
  searchable, and renameable without changing the UUID.
- TAC `rename`, `check`, and member-scoped literal `search` commands.
- A copy-first migration tool for existing string TAC identifiers.
- Unicode-consistent literal TAC search across names, descriptions, summaries,
  subjects, and bodies, including NFC/NFD-equivalent text.

### Changed

- MCP responses preserve structured error codes and details from tabd.
- TAC commands address converted topics by UUID. Names are used for creation,
  display, and search.
- `tac show --node` advances only the returned deliveries to `INJECTED`.
  It does not mark them `READ`, and older unreturned messages may still block a send.
- The pending count printed after a send is scoped to that sender and recipient.
  `tabc who` remains the total unread view.
- Terminal route capture now validates the native foreground Codex or Claude CLI
  at a detached session boundary. Unknown launch modes and mismatched terminals
  fail closed. New or moved routes keep automatic Enter off unless explicitly
  enabled with `register --auto-enter on`.

### Protocol details

- A fresh 0.2.0 ledger mints canonical lowercase, hyphenated UUIDs for TACs.
  Existing ledgers retain string TAC IDs until explicit conversion.
- A TAC name is trimmed at its edges and compared as
  `NFC(casefold(NFC(name)))`. Control, format, surrogate, private-use, unassigned,
  and non-ordinary whitespace characters are refused. Unicode category results
  follow the Python version running tabd.
- A partially converted ledger that has identity columns but still contains
  string TAC IDs refuses TAC creation and rename with `TAC_NOT_CONVERTED`.
- Missing canonical TAC IDs return `TAC_NOT_FOUND`. Read-only name lookup can
  report `exists: false`; names are not accepted where a TAC UUID designates the
  target.
- Structured server refusal codes and retry rules are listed in
  [GUIDE.md](GUIDE.md#refusal-codes). MCP preserves those fields without truncation.

### Upgrade note

An upgrade from 0.1.6 preserves nodes, messages, delivery states, TAC membership,
names, and labels. Package installation and server startup do not convert TAC IDs.
Pre-0.2.0 ledgers keep their string TAC identifiers until explicitly converted.
Stop `tabus.doorbell` and `tabd`. Confirm that `<ledger.db>-wal` is absent or
empty (0 bytes). A remaining empty WAL or SHM file is safe. If the WAL is not
empty, start `tabd` and stop it cleanly, then check again. Back up the main
database only after the WAL is absent or empty. Install the required package,
then run a read-only preflight and write a separate converted database:

```bash
python -m pip install --upgrade "tabc==0.2.0"
# Or, for desktop MCP support:
python -m pip install --upgrade "tabc[mcp]==0.2.0"

python -m tabus.tac_migration <ledger.db>
python -m tabus.tac_migration <ledger.db> --output <converted.db>
```

Replace only the main database file after the row counts and converted database
are verified. Do not replace it while its WAL file is non-empty. A remaining
empty WAL or SHM file is safe. Restart `tabd` first, then restart
`tabus.doorbell`. Do not run old and new daemons against the same active ledger
during the swap.
After conversion, commands that use a TAC name instead of its UUID are refused
with `TAC_ID_INVALID`. Scripts must use the UUID printed by `tabc tac ls`.
After restart, use `tabc --version` and `tabd --version` to confirm that both
commands resolve to 0.2.0.

When replacing an installed notifier, stop the old `tabus.doorbell` first, restart
`tabd`, then start the new notifier. See the registration notes in
[README.md](README.md#node-identity-and-signing-keys).
