#!/usr/bin/env bash
# Bring up a throwaway bus for the README cast, so recording the demo never
# touches a real mailbox. demo.tape sources this before it starts typing; you
# can also source it in any shell to try the same flow by hand.
#
# It puts all state under a fresh mktemp TABC_HOME and runs a private daemon on
# a spare port, then registers two example nodes, alice and bob.
set -e

export TABC_HOME="$(mktemp -d)/tabc-demo"
export TABC_BUS_URL="http://127.0.0.1:8799"
mkdir -p "$TABC_HOME"

# Free the demo port from any earlier render, then start a private daemon for
# this demo only. It shares nothing with a real tabd.
lsof -ti tcp:8799 2>/dev/null | xargs kill 2>/dev/null || true
sleep 0.3
tabd --bind 127.0.0.1 --port 8799 >/dev/null 2>&1 &

# Wait until the daemon accepts a signed request.
for _ in $(seq 25); do
  TABC_NODE=alice tabc who >/dev/null 2>&1 && break
  sleep 0.2
done

# Two nodes on one machine. Each carries its own identity on every command, so
# no shared default is needed.
TABC_NODE=alice tabc register --node alice --kind claude >/dev/null 2>&1
TABC_NODE=bob   tabc register --node bob   --kind claude >/dev/null 2>&1
TABC_NODE=bob   tabc beat     --node bob   >/dev/null 2>&1

clear
