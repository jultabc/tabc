# Changelog

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
