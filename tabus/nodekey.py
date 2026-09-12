#!/usr/bin/env python3
"""nodekey — node identity as an ed25519 keypair.

The public key (base58) *is* the node key. The private key never leaves the machine.

This replaces the shared-secret model:
  - shared token: proves identity by *sending* the secret. Intercept it and you can
    impersonate. Transmission is the weakness.
  - node key:     the private key is never sent. The node signs locally and the
    server verifies with the public key. Seeing every message and every signature
    still does not let you forge a new one. Strictly stronger.

Key storage v1 is a local file (0600, gitignored). OS keychain / Secure Enclave is
a later layer.

Two rules that are not negotiable:
  - Never commit or share a private key. Only the public key is registered and sent.
  - verify() never raises. Live paths call it. A failed check returns False.

Uses pyca/cryptography for ed25519. base58 is implemented here to keep the
dependency surface small. Storage paths are injected so tests can use their own.
"""

import hashlib
import os

from . import paths

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

# base58, Bitcoin/Solana alphabet — no 0 O I l, so keys don't misread by eye.
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58)}

KEY_DIR = paths.home()
KEY_FILE = os.path.join(KEY_DIR, ".node_key")  # legacy default; tests pass a path
SHORT_LEN = 13  # display prefix. The full key is the authority (like a git short hash)


def key_path(node_id: str, base_dir: str = None) -> str:
    """One key file per node, so sessions on the same machine cannot impersonate
    each other.

    With a single per-machine key, two sessions share it and either one can produce
    the other's signature. We reproduced exactly that. Splitting per node means a
    typo like `--sender alice` from bob's session gets signed with bob's key and
    fails verification — it stops honest mistakes, not just attacks.

    What this does not stop: a user deliberately reading someone else's key file on
    the same machine. File permissions cannot prevent that; OS-level protection is
    a later layer.

    node_id is restricted to safe characters so it cannot escape the directory.
    """
    base = base_dir or KEY_DIR
    # Reject loudly instead of sanitizing quietly. '/' and '.' allow path traversal,
    # and quiet stripping can collapse two different node_ids onto one file.
    # Real node_ids are alphanumeric, so this rejects nothing in practice.
    if not is_safe_node_id(node_id):
        raise ValueError(f"node_id is not safe as a filename (alnum/-/_ only): {node_id!r}")
    return os.path.join(base, f".node_key.{node_id}")


def is_safe_node_id(node_id) -> bool:
    """Whether a node id is safe as a filename AND as a display string.

    🔴 One rule, two callers. key_path used it to keep a name from escaping the key
    directory; the server (bus_register) uses the same predicate so a name it accepts
    can never be one the key path would reject — two places, one rule, so they cannot
    drift apart. ASCII letters, digits, dash, underscore only: that excludes
    whitespace and control characters (newline included), which stops a crafted name
    from carrying an Enter into the doorbell's auto-typed line, AND excludes non-ASCII
    look-alikes (Cyrillic 'а' vs Latin 'a') so a name cannot impersonate another on a
    doorbell line by homograph. isalnum() alone is Unicode and would let those through,
    so "display string" would be an over-claim; the ascii() guard makes it true.
    """
    return (
        isinstance(node_id, str)
        and bool(node_id)
        and all((c.isascii() and c.isalnum()) or c in "-_" for c in node_id)
    )


def b58encode(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    out = ""
    while n > 0:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    pad = len(b) - len(b.lstrip(b"\x00"))  # leading zero bytes encode as '1'
    return "1" * pad + out


def b58decode(s: str) -> bytes:
    n = 0
    for c in s:
        if c not in _B58_INDEX:
            raise ValueError(f"not a base58 character: {c!r}")
        n = n * 58 + _B58_INDEX[c]
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + body


def _load_priv(path: str) -> Ed25519PrivateKey:
    with open(path, "rb") as f:
        raw = f.read()
    if len(raw) != 32:  # a raw private key is 32 bytes; anything else is damaged
        raise ValueError(f"node key file is corrupt (expected 32 bytes, got {len(raw)}): {path}")
    return Ed25519PrivateKey.from_private_bytes(raw)


def _create_priv(path: str) -> Ed25519PrivateKey:
    priv = Ed25519PrivateKey.generate()
    # Create at 0600 atomically (O_EXCL): lock permissions first, then write.
    # If the file already exists this does not overwrite it — safe under a race.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, priv.private_bytes_raw())
    finally:
        os.close(fd)
    return priv


def ensure_key(path: str = KEY_FILE) -> Ed25519PrivateKey:
    """This machine's private key, created at 0600 if absent.

    Returns the key object. It does not leave this module.
    """
    if os.path.exists(path):
        return _load_priv(path)
    return _create_priv(path)


def public_key_b58(path: str = KEY_FILE) -> str:
    """This machine's node key: the full public key in base58, for registration."""
    return b58encode(ensure_key(path).public_key().public_bytes_raw())


def short_id(node_key_b58: str, n: int = SHORT_LEN) -> str:
    """A display prefix. Callers lengthen it on collision, like a git short hash.
    Decisions must use the full key.
    """
    return node_key_b58[:n]


def sign(msg: bytes, path: str = KEY_FILE) -> bytes:
    """Sign with this machine's private key (64 bytes). Only the signature travels."""
    return ensure_key(path).sign(msg)


def verify(node_key_b58: str, msg: bytes, sig: bytes) -> bool:
    """Verify a signature against a node key (base58 public key).

    Forged, tampered, and malformed input all return False. This never raises —
    live paths call it, and "could not verify" is the safe answer to every failure.
    """
    try:
        pub = Ed25519PublicKey.from_public_bytes(b58decode(node_key_b58))
        pub.verify(sig, msg)
        return True
    except Exception:
        return False


def canonical_request(node: str, method: str, path: str, body: str, ts: str) -> bytes:
    """The canonical bytes for authenticating a whole request by node signature.

    This is what replaces the shared bus token: instead of a secret everyone holds,
    each request carries the acting node's signature over these bytes, and the server
    verifies it against that node's registered public key. A signature transmits no
    secret, so intercepting one does not let anyone forge another request.

    Binds: node (who is acting), method + path (which endpoint, including the query
    string), body (the exact payload), and ts (unix-seconds as a string, for replay
    defense — the server rejects a ts outside its window, so a captured request
    cannot be replayed later). Each field is hashed separately then joined, so a
    separator inside any field cannot shift the boundaries and let a different
    request pass under one signature.

    This is the single definition both the signer (client) and the verifier (server)
    derive, so the two cannot drift apart.
    """

    def h(s: str) -> str:
        return hashlib.sha256((s or "").encode("utf-8")).hexdigest()

    return "\n".join(
        [h(node), h(method), h(path), h(body), h(str(ts))]
    ).encode("utf-8")
