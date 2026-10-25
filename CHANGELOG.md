# Changelog

## Unreleased

### Added

- `tabc send --id <UUID>` sends with a caller-chosen message id, so a retry
  after a lost response is not stored twice. A resend goes through the
  usual send checks first, so unread mail can refuse it with
  `UNREAD_BLOCKED`. After those checks, the same id with the same
  envelope prints `stored id=<id>` and, below it,
  `identical envelope resent — not stored twice`, and exits 0. With a
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
