"""PCIS-style signing surfaces for Roomcomm.

Inspired by `liars-demo-v1.1-final/pcis_sign.py`. Provides four canonical
surfaces; all use Ed25519. The naming convention surface_X_canonical(...)
returns the exact bytes signed/verified, so signer and verifier cannot drift.

Surfaces:

  1. message: sign(text || ts_iso || room_uuid || memory_root)
     - Signed by the message *author* (agent's own key).
     - memory_root may be "" if the agent has no memory commitment.

  2. revision: sign(claim_id || kind || value)
     - Signed by the agent posting a manual revision (for confirm/contradict
       digital ack). Optional.

  3. handshake: sign(context_hash_hex)
     - Signed by the agent finalising the room with a 2-party handshake.

  4. arbiter_row: sign(canonical_revision_payload)
     - Signed by the *platform's* arbiter key on every revision insert.
     - Combined with prev_hash + row_hash creates a tamper-evident ledger.

The arbiter key lives at /etc/roomcomm/arbiter.key (32-byte seed, chmod 600).
On first start the bootstrap function generates one if missing.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Optional

import nacl.signing
import nacl.exceptions

def _default_key_path() -> Path:
    """Default arbiter key location. Prefers /etc/roomcomm/arbiter.key for
    production; falls back to ./data/arbiter.key for dev/tests where /etc
    is read-only or not Linux."""
    sys_path = Path("/etc/roomcomm/arbiter.key")
    try:
        sys_path.parent.mkdir(parents=True, exist_ok=True)
        return sys_path
    except (OSError, PermissionError):
        # Dev fallback: alongside the SQLite database.
        return Path(__file__).resolve().parent.parent / "data" / "arbiter.key"


ARBITER_KEY_PATH = Path(os.environ.get(
    "ROOMCOMM_ARBITER_KEY_PATH", str(_default_key_path())
))


# ---------- Generic primitives ----------

def generate_keypair() -> tuple[bytes, bytes]:
    """Return (32-byte seed, 32-byte pubkey)."""
    sk = nacl.signing.SigningKey.generate()
    return bytes(sk), bytes(sk.verify_key)


def sign(privkey_seed: bytes, message: bytes) -> bytes:
    """Sign message bytes with the 32-byte seed. Returns 64-byte signature."""
    sk = nacl.signing.SigningKey(privkey_seed)
    return bytes(sk.sign(message).signature)


def verify(pubkey: bytes, message: bytes, sig: bytes) -> bool:
    """Returns True iff sig is valid for message under pubkey. Fail-closed."""
    try:
        vk = nacl.signing.VerifyKey(pubkey)
        vk.verify(message, sig)
        return True
    except (nacl.exceptions.BadSignatureError, Exception):
        return False


def verify_hex(pubkey_hex: str, message: bytes, sig_hex: str) -> bool:
    """Hex-string variant. Returns True iff valid."""
    try:
        pubkey = bytes.fromhex(pubkey_hex)
        sig = bytes.fromhex(sig_hex)
    except (TypeError, ValueError):
        return False
    return verify(pubkey, message, sig)


# ---------- Canonical surfaces ----------

def message_surface(text: str, ts_iso: str, room_uuid: str, memory_root: Optional[str]) -> bytes:
    """Bytes signed by message author. memory_root may be None → encoded as ''."""
    return (text + ts_iso + room_uuid + (memory_root or "")).encode("utf-8")


def revision_surface(claim_id: str, kind: str, value: str) -> bytes:
    """Bytes signed by agent on a manual revision."""
    return f"{claim_id}|{kind}|{value}".encode("utf-8")


def handshake_surface(context_hash_hex: str) -> bytes:
    """Bytes signed by agent on final handshake."""
    return context_hash_hex.encode("ascii")


def iso_canonical(dt) -> str:
    """Canonical ISO-Z formatting for timestamps in signed payloads.

    Both signer and verifier MUST use this exact format. Microseconds always
    included (six digits) so that rounding never differs between insert and
    verify paths.
    """
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def revision_canonical_payload(
    *,
    claim_id: str,
    revision_id: int,
    kind: str,
    value: str,
    author_agent_id: str,
    source_msg_id: Optional[int],
    created_at_iso: str,
    prev_hash: str,
) -> str:
    """Canonical JSON that goes into the hash chain AND is signed by arbiter.

    Stable across implementations: sorted keys, no whitespace, UTF-8.
    Identical bytes → identical signature → reproducible verification.
    """
    payload = {
        "claim_id": claim_id,
        "revision_id": revision_id,
        "kind": kind,
        "value": value,
        "author_agent_id": author_agent_id,
        "source_msg_id": source_msg_id,
        "created_at": created_at_iso,
        "prev_hash": prev_hash,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def row_hash(prev_hash: str, canonical_payload: str) -> str:
    """sha256(prev_hash || canonical_payload), hex."""
    return hashlib.sha256((prev_hash + canonical_payload).encode("utf-8")).hexdigest()


# ---------- Arbiter key bootstrap ----------

_arbiter_seed: Optional[bytes] = None
_arbiter_pubkey: Optional[bytes] = None


def _ensure_loaded() -> None:
    """Load the arbiter key from disk; generate one on first start if missing.

    Stored as a 32-byte seed (raw bytes), chmod 600. The path is fixed by
    ROOMCOMM_ARBITER_KEY_PATH env (default /etc/roomcomm/arbiter.key).
    """
    global _arbiter_seed, _arbiter_pubkey
    if _arbiter_seed is not None:
        return
    p = ARBITER_KEY_PATH
    if p.exists():
        seed = p.read_bytes()
        if len(seed) != 32:
            raise RuntimeError(
                f"arbiter key at {p} has wrong length: got {len(seed)}, want 32"
            )
    else:
        seed, _ = generate_keypair()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(seed)
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass  # best effort on platforms without POSIX perms
    _arbiter_seed = seed
    _arbiter_pubkey = bytes(nacl.signing.SigningKey(seed).verify_key)


def arbiter_pubkey_hex() -> str:
    _ensure_loaded()
    assert _arbiter_pubkey is not None
    return _arbiter_pubkey.hex()


def arbiter_sign(message: bytes) -> bytes:
    _ensure_loaded()
    assert _arbiter_seed is not None
    return sign(_arbiter_seed, message)


def arbiter_sign_hex(message: bytes) -> str:
    return arbiter_sign(message).hex()
