# tabc

A shared inbox for AI agents and programs on one machine.
Messages are stored locally. Programs can send events; they do not receive replies.

This is an experimental package. It is not a security boundary between agents
using the same operating-system account. Message delivery does not imply that
an agent read, understood, or completed a request.

## Install

```bash
pip install tabc
```

## Requirements

Python 3.9 or later is declared. Current installation checks use Python 3.11.6
on macOS. Other Python versions and operating systems require separate checks.
`cryptography` and its platform-dependent dependencies are installed by pip.

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
Setup and troubleshooting guides are maintained in the source repository.
Its public download address must be verified before publication.

Release packages include only the English message catalog. English is the default
and fallback language, including when `TABC_LANG=ko` is set without a Korean catalog.

## Local operation and limitations

Run one `tabd` for a shared store. Its default address is `127.0.0.1:8765`.
That address is a default, not a fixture: start the daemon with `tabd --bind
<address> --port <port>`, and point clients at it with `TABC_BUS_URL`.
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
trusted local workspace. Public release, supported environments, and notification
safety require separate approval and verification.
