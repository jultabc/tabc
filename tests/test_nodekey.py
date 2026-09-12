#!/usr/bin/env python3
"""nodekey tests: generation, persistence, signature verification, forgery rejection, invalid-input safety, uniqueness, and permissions.

Live notifier/bus callers require verify to return True/False for any input without raising.
Private key files use mode 0600, persist across calls, and differ across devices. Temporary paths only; no live data.
Run directly and check the exit code."""

import os
import stat
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tabus import nodekey as nodekey  # noqa: E402

fails = []


def check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        fails.append(name)


def _raises_value(f, *a):
    """Return True if f(*a) raises ValueError; False for other exceptions or no exception."""
    try:
        f(*a)
        return False
    except ValueError:
        return True
    except Exception:
        return False


TMP = tempfile.mkdtemp(prefix="nodekey_")
P1 = os.path.join(TMP, "a.key")
P2 = os.path.join(TMP, "b.key")

# Base58 round trip.
print("── base58 ──")
for raw in [b"\x00\x00\x01\x02", b"hello tabus", bytes(range(32)), b"\x00" * 3 + b"x"]:
    check(f"1 b58 round trip {raw[:6]!r}", nodekey.b58decode(nodekey.b58encode(raw)) == raw)
check(
    "1b b58decode('0') raises ValueError (0 is outside the alphabet)",
    _raises_value(nodekey.b58decode, "0"),
)

# Generation, permissions, and persistence.
print("Generation, permissions, and persistence")
check("2 file is initially absent", not os.path.exists(P1))
nk1 = nodekey.public_key_b58(P1)
check("2 ensure creates the file", os.path.exists(P1))
mode = stat.S_IMODE(os.stat(P1).st_mode)
check(f"2 file mode is 0600 (observed {oct(mode)})", mode == 0o600)
check("2 private key file contains 32 bytes", os.path.getsize(P1) == 32)
nk1_again = nodekey.public_key_b58(P1)
check("3 repeated calls return the same node key (persistent)", nk1 == nk1_again)

# Node key format.
print("Node key format")
check(f"4 base58 node key length is 43-44 (observed {len(nk1)})", 43 <= len(nk1) <= 44)
check("4 first 13 characters are a prefix of the full key", nodekey.short_id(nk1) == nk1[:13])
check("4 short defaults to 13 characters", len(nodekey.short_id(nk1)) == 13)

# Uniqueness across devices.
print("Uniqueness")
nk2 = nodekey.public_key_b58(P2)
check("5 different paths (devices) produce different node keys", nk1 != nk2)

# Signing and verification (trust anchor).
print("Signing and verification (trust anchor)")
msg = b"erin: SELL AAPL 10 @ market"
sig = nodekey.sign(msg, P1)
check("6 signature contains 64 bytes", len(sig) == 64)
check(
    "6 signing with my key verifies against my node key", nodekey.verify(nk1, msg, sig) is True
)

# Forgery rejection.
print("Forgery rejection")
check(
    "7 tampered message returns False",
    nodekey.verify(nk1, b"erin: SELL AAPL 100 @ market", sig) is False,
)
check("7 verification against another node key returns False", nodekey.verify(nk2, msg, sig) is False)
sig2 = nodekey.sign(msg, P2)
check("7 verifying P2's signature with nk1 returns False", nodekey.verify(nk1, msg, sig2) is False)

# verify returns False for invalid input without raising.
print("── verify crash-proof ──")
check(
    "8 invalid base58 node key returns False without raising", nodekey.verify("0OIl!!", msg, sig) is False
)
check("8 empty node key returns False", nodekey.verify("", msg, sig) is False)
check("8 short node key (wrong length) returns False", nodekey.verify("abc", msg, sig) is False)
check("8 empty signature returns False", nodekey.verify(nk1, msg, b"") is False)
check("8 invalid signature returns False", nodekey.verify(nk1, msg, b"\x00" * 64) is False)

# Corrupt key file detection.
print("Corrupt key files")
P3 = os.path.join(TMP, "c.key")
with open(P3, "wb") as f:
    f.write(b"not-32-bytes")
check(
    "9 a key file not containing 32 bytes raises ValueError (no silent regeneration)",
    _raises_value(nodekey.ensure_key, P3),
)

print(f"\n{'❌ FAIL' if fails else '✅ ALL PASS'} — {len(fails)} failed")
sys.exit(1 if fails else 0)
