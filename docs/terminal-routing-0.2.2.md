# Terminal routing in 0.2.2

Status: review candidate. Installed runtime has not been replaced.

Registration normalizes `kind` to lowercase. Codex with automatic Enter enabled
retains the Codex queue selection and its existing fallback rules. Other kinds
use the common iTerm2 or tmux route. No OpenCode-specific adapter is added.

For detached command children, only the immediate session-boundary parent may
provide terminal evidence. It must belong to the same OS user, own the foreground
process group, and read standard input from the expected terminal. Process state,
executable path, and standard input are checked again before accepting it. The
route resolver must still match the target terminal. Product names and install
directories are not an admission list.

This verifies terminal ownership, not whether a program understands a message.
A foreground program with terminal standard input can qualify even if it is not
an agent. Automatic Enter remains a separate opt-in. Do not register a node from
a terminal that will subsequently be used for an unrelated program.

Existing node keys, mail, receipts, and stored routes are not migrated. A failed
capture does not replace an existing route. Re-register from the intended
terminal after installing the reviewed release, then verify notification delivery.

Validated development checks:

```sh
python -B -m unittest discover -s tests -p test_terminal_frontend.py
python -B -m unittest discover -s tests -p test_codex_queue.py
python -B tests/test_route_capture_provenance.py
python -B tests/test_version_agreement.py
```

No daemon restart, broker operation, or data deletion is part of these checks.
