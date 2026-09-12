# Contributing

## Set up a checkout

Python 3.9 or newer, and `cryptography` — every request is authenticated
by an ed25519 signature, so the daemon will not import without it.
Storage is SQLite from the standard library.

```bash
pip install -e .
```

The editable install matters while developing: the `tabc` and `tabd` entry
points import directly from this checkout. A source edit is visible to the next
process without reinstalling. Restart a running daemon or doorbell after changing
code it imports, and rerun the install when `pyproject.toml` changes.

The tests need nothing else. HTTP tests start their own daemon on an isolated
port, point it at a temporary `TABC_HOME`, and register the node keys they need.
There is no shared token to export and no daemon to start first.

To poke at the bus by hand, start one yourself:

```bash
python3 -m tabus.daemon --bind 127.0.0.1 --port 8765
```

## Running the tests

Tests are standalone scripts rather than a pytest suite. Run all of them with:

```bash
for f in tests/test_*.py; do python3 "$f"; done
```

Each script exits non-zero on failure. While iterating, run the test closest to
the code you changed first, then use a fail-fast loop:

```bash
python3 tests/test_request_auth.py
for f in tests/test_*.py; do python3 "$f" || break; done
```

The Java client keeps its fast HTTP-fixture tests separate from the cross-language
boundary. Run both from its directory:

```bash
cd clients/java
./gradlew test
TABC_TEST_PYTHON=python3 ./gradlew integrationTest
```

`integrationTest` starts the repository's real Python `tabus.daemon` with a
temporary database and node-key directory. It sends through the real Java client,
checks the recipient inbox, then proves that a request signed with the wrong
node key is refused and not stored. Set `TABC_TEST_PYTHON` to the interpreter
where this checkout's Python dependencies are installed. The test never reads or
writes the live inbox. It imports the Python daemon from this checkout through
`PYTHONPATH`; it does not inspect an installed daemon listening on port 8765, so
this is a source-contract test rather than deployment validation.

### When a test fails

Most scripts finish against a temporary database. `test_acceptance.py` starts a
real daemon and drives the command line end to end, so it takes longer and
reports scenario failures together. When sharing a failure, include the commit
and the complete summary instead of a colour or a remembered count:

```bash
git rev-parse --short HEAD
python3 tests/test_acceptance.py
```

## Checking the docs

```bash
python3 scripts/link_check.py --strict
```

`link_check` walks every HTML and Markdown file and reports links whose
target does not exist. It counts places to fix rather than distinct URLs,
because the same broken path in three files is three edits.

`doc_check` reads one HTML page: the three theme layers, an explicit body
background, colour literals written outside the token set, token
definitions whose value was damaged, `var()` with no definition, tags left
unbalanced, and anchors pointing at nothing. Arguments that are not HTML
are named and skipped rather than checked, so a mistyped glob is visible.

Both commands above exit non-zero on a finding. Without `--strict`,
`link_check.py` is an informational report and exits zero even when it lists
broken paths.

A green result is narrower than it sounds: it says the page renders and its
links resolve. It does not say the page is true. A name that should not be
public, a stale issue number, and a figure that was right last week all
pass both scripts. Those are read, not measured.

## Follow a request through the code

The package is deliberately split by responsibility:

| File | Owns |
|---|---|
| `tabus/paths.py` | the `TABC_HOME` state directory |
| `tabus/nodekey.py` | per-node keys and the canonical signed request |
| `tabus/bus.py` | schema, migrations, and message or delivery operations |
| `tabus/daemon.py` | HTTP authentication, authorization, and route dispatch |
| `tabus/cli.py` | argument parsing, request signing, and CLI output |
| `tabus/doorbell.py` | terminal notification; it reads the message store but does not write it |

A normal CLI request starts in `COMMANDS` in `tabus/cli.py`, calls its handler,
maps to an HTTP path through `MAPPING`, passes authentication and any self-scope
check in `tabus/daemon.py`, and ends in a `bus_*` operation. Keep policy in the
bus or daemon layer rather than copying it into an adapter.

### Adding a CLI command

1. Put the store operation in `tabus/bus.py` when the command reads or mutates
   bus state. Keep transaction and delivery-state rules there.
2. Add the HTTP route in `tabus/daemon.py`. Decide explicitly whether it is
   self-scoped, a member-scoped read, or a management action; a valid signature
   authenticates a node but does not by itself authorize every target.
3. Add the route to `MAPPING`, write a small `fn_*` client handler, and add the
   command to `COMMANDS` in `tabus/cli.py`. Pass the acting node to every nested
   `call()` rather than relying on ambient identity when the command already has
   `--node` or `--sender`.
4. Test the bus operation directly, then exercise the real client and daemon
   together with a temporary `TABC_HOME`, `TABC_DB`, and port. Include the
   wrong-node and missing-identity cases, not only the successful request.

Aliases should reuse the existing `COMMANDS` entry instead of copying its
arguments. `dm` is the example: `COMMANDS["dm"] = COMMANDS["send"]` makes the
two names share one handler and one option specification.

### Wire compatibility and signatures

Release and protocol versions are separate. Read both with `tabc --version` or
`tabd --version`; their source of truth is `tabus/bus.py`. Change the protocol
version when the stored or transmitted message envelope changes, not for every
CLI wording change or new endpoint.

Every HTTP request carries `X-Node`, `X-Node-Ts`, and `X-Node-Sig`. The signature
covers the acting node, method, exact path including its query string, exact JSON
body text, and timestamp. An integration must sign the text it actually sends;
re-serializing JSON or changing query ordering after signing produces a different
request and is rejected. `tabus.nodekey.canonical_request` is the only canonical
form, and `tabus.cli.call` is the reference implementation.

There is no shared token. First registration bootstraps by verifying the request
against the public key in that same registration body. Later requests, including
re-registration, are checked against the key already pinned to the node id. Key
replacement is the local `rotate-key` operator path, not a variation of register.

### Keeping tests off a live inbox

Set environment variables before importing `tabus`; `bus.py` and the key module
resolve some paths at import time.

```python
import os
import tempfile

test_home = tempfile.mkdtemp(prefix="tabc_test_")
os.environ["TABC_HOME"] = test_home
os.environ["TABC_DB"] = os.path.join(test_home, "tabus.db")
os.environ.pop("TABC_NODE", None)

import tabus
```

The default message database is `TABC_HOME/tabus.db`. Per-node private keys,
`host_id`, and `user_email` stay under `TABC_HOME`; `TABC_DB` moves only the
message database. Current clients ignore the legacy `TABC_HOME/node` file, so a
test must pass an acting node or set `TABC_NODE` instead of creating that file.

## Making a change

Work on a branch and open a pull request. The main branch takes merges
only.

Write commit messages and pull request text in English, including the body.
Future maintainers will often meet the reasoning in the history before they meet
the original author.

Keep the subject line about what changed. Put the reasoning in the body,
and say what you measured rather than what you believe.

## Conventions

Comments, docstrings and documentation are in English.

Do not put personal names, absolute paths, internal issue numbers, or
email addresses in anything committed here. Example node names follow
the `alice` / `bob` convention the tests already use.

## Questions reviewers usually ask

### Why check both the signature and the node in the payload?

They answer different questions. The signature proves which node sent the
request. The payload or query names the inbox, sender, or catch-up target. A
self-scoped route must require those values to match, or a correctly signed node
could act on another node's state.

### Why did my monkeypatch of `tabus.bus_send` do nothing?

`tabus/__init__.py` re-exports bus functions for compatibility, but code inside
`tabus.bus` resolves its own module globals. Patch `tabus.bus.bus_send` when a
test needs to replace the function used by that module.

### Should a headless client use `--program`?

Only when it is a send-only event source. A normal node can send and pull without
a terminal; it simply has no doorbell route. `register` accepts a tmux or iTerm
route only when it matches the process terminal or a same-session ancestor.
Inherited environment variables alone are not proof. `--program` registers a
send-only identity: incoming envelopes are refused. Use a separate name; an
existing agent cannot become a program. Existing programs may re-register.

### Does `register` configure `TABC_NODE`?

No child process can change its parent shell's environment. `register` registers
the node, creates its key if needed, and checks the available terminal route.
It does not change the tab title. Pass the acting identity explicitly with
`--node` or `--sender` as the command requires.
