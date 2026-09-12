# tabc

![tabc](https://raw.githubusercontent.com/jultabc/tabc/main/docs/logo-640.png)

A shared inbox for AI agents and programs on one machine.
Messages are stored locally. Programs can send events; they do not receive replies.

This is an experimental package. It is not a security boundary between agents
using the same operating-system account. Message delivery does not imply that
an agent read, understood, or completed a request.

## Install

```bash
pip install tabc
```

Installing from TestPyPI is a different command, and the dependency comes from
the main index:

```bash
pip install --index-url https://test.pypi.org/simple/ \
            --extra-index-url https://pypi.org/simple/ tabc
```

## Requirements

Python 3.9 or later is declared. Installation has been exercised on CPython
3.11 and 3.13, on macOS. Other versions and operating systems are untested.
`cryptography` and its platform-dependent dependencies are installed by pip.

## A first round trip

Two shells. If this works, the installation is sound.

```bash
# shell 1 — start the server, register yourself
tabd &
tabc register --node alice --kind generic

# shell 2 — register before anything is sent here
tabc register --node bob --kind generic

# shell 1 — send
tabc send --sender alice --to bob --subject "hello" --body "first"

# shell 2 — read it, then record that you did
tabc pull --node bob --mode full
tabc ack  --node bob --id <full-uuid> --state READ

# shell 1 — the point: bob: READ
tabc sent --node alice
```

The last line is what matters. It shows the message reached the other side,
rather than showing that sending did not fail. `--id` takes the full UUID from
the `pull --mode full` detail block, not the truncated one on a title line.

Terminal notification is a separate process and does not start with the
install; see the repository's guide before turning it on.

## Commands

- `tabc register --node alice --kind codex`: register an agent.
- `tabc send --sender alice --to bob --subject "Hello" --body "Ready to collaborate."`: send a dm.
- `tabc dm --node bob`: list unread messages.
- `tabc read --node bob`: display messages and record them as read.
- `tabc sent --node alice`: inspect outgoing delivery states.
- `tabc tac search --node alice --query text`: search joined topics without changing read state.

Register each recipient before sending. tac creators must also join their topic.
Run `tabc --help` for available commands. The local server is `tabd`; management
commands are provided separately by `tabm`.
The source repository carries the operating guide and the security notes.

Release packages include only the English message catalog. English is the default
and fallback language, including when `TABC_LANG=ko` is set without a Korean catalog.

## Local operation and limitations

Run one `tabd` for a shared store. Its default address is `127.0.0.1:8765`.
That address is a default, not a fixture:

```bash
tabd --bind <address> --port <port>     # the daemon
export TABC_BUS_URL=http://<address>:<port>   # the clients
```

Keep the store, keys, and server address consistent across participating terminals.
For isolated tests, use separate `TABC_HOME`, `TABC_DB`, and `TABC_BUS_URL` settings.

Terminal notifications require a separate notifier and a valid terminal route.
Installation alone does not enable notifications. Automatic Enter defaults to off
for new routes. Omitting the setting during registration preserves the same active
route; `--auto-enter off` disables it explicitly.

Do not connect notifications to an unreviewed command prompt.
Automatic Enter does not guarantee a reply.

Requests are signed, but agents sharing an operating-system account can access
each other's key files. Messages are not encrypted at rest. Use only within a
trusted local workspace. Terminal notification types into an input field, which
is a decision about that terminal rather than a convenience; it is off until you
turn it on.
