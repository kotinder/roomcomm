"""Per-subject daily quotas — the accounting layer of "open join, keyed create".

A *subject* is who we bill a request to: ``key:<id>`` when the caller presents
a valid Bearer key, ``ip:<addr>`` otherwise. Counters live in the
``usage_counters`` table (persistent across restarts, unlike the in-memory
burst limiters in main) and are incremented atomically via
``INSERT ... ON CONFLICT DO UPDATE``.

Used by BOTH transports — REST handlers in main.py and MCP tools in
mcp_server.py — so this module must not import either of them. Errors are
raised as transport-neutral exceptions; each caller converts them to its own
error shape (HTTPException / MCP tool error).

QUOTA_MODE:
    soft  (default) — meter everything, never reject. Rollout phase 0.
    hard            — reject over-quota requests. Rollout phase 1+.
"""

import hashlib
import logging
import os
import secrets
from typing import Optional

from sqlmodel import Session, select

from .models import AgentKey, Room, utcnow

log = logging.getLogger("roomcomm.quota")

# Rollout switch: flipped to "hard" via env when phase 1 starts. Tests and the
# admin may also flip the module attribute directly.
QUOTA_MODE = os.environ.get("QUOTA_MODE", "soft").lower()

# Daily quotas per tier, (msg, room). Env-tunable so calibration after the
# soft phase is a config change, not a deploy.
TIER_QUOTAS: dict[str, tuple[int, int]] = {
    "anon": (
        int(os.environ.get("QUOTA_ANON_MSG", 30)),
        int(os.environ.get("QUOTA_ANON_ROOM", 3)),
    ),
    "free": (
        int(os.environ.get("QUOTA_FREE_MSG", 500)),
        int(os.environ.get("QUOTA_FREE_ROOM", 20)),
    ),
    "verified": (
        int(os.environ.get("QUOTA_VERIFIED_MSG", 2000)),
        int(os.environ.get("QUOTA_VERIFIED_ROOM", 50)),
    ),
    # trusted has no ceiling unless the key carries a per-key override.
    "trusted": (10**9, 10**9),
    "blocked": (0, 0),
}

KEY_PREFIX = "rk_"        # agent keys: rk_<token>
ROOM_KEY_PREFIX = "wk_"   # room write-keys: wk_<token>

GET_KEY_HINT = "get a free key: POST https://roomcomm.xyz/api/keys {agent_id}"


class AuthError(Exception):
    """Presented key is malformed, unknown, revoked, or blocked."""

    def __init__(self, detail: str, status: int = 401):
        super().__init__(detail)
        self.detail = detail
        self.status = status


class QuotaExceeded(Exception):
    """Subject is over its daily budget (raised only in hard mode)."""

    def __init__(self, detail: str, retry_after: int):
        super().__init__(detail)
        self.detail = detail
        self.retry_after = retry_after


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def generate_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def generate_room_key() -> str:
    return ROOM_KEY_PREFIX + secrets.token_urlsafe(16)


def generate_verify_code() -> str:
    # Short enough to paste into a Telegram chat, long enough not to guess.
    return secrets.token_hex(6)


def resolve_key(session: Session, authorization: Optional[str]) -> Optional[AgentKey]:
    """Map an Authorization header to an AgentKey, or None when absent.

    A *presented but bad* key is an explicit error, never a silent downgrade
    to anonymous — the caller must learn their key is dead, not quietly burn
    the IP budget.
    """
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise AuthError("malformed Authorization header, expected: Bearer <key>")
    if not token.startswith(KEY_PREFIX):
        raise AuthError("unknown key format")
    key = session.exec(
        select(AgentKey).where(AgentKey.key_hash == hash_key(token))
    ).first()
    if key is None:
        raise AuthError("unknown key")
    if key.revoked:
        raise AuthError("key revoked", status=403)
    if key.tier == "blocked":
        raise AuthError("key blocked", status=403)
    key.last_used_at = utcnow()
    session.add(key)
    return key


def check_write_policy(room: Room, provided_room_key: Optional[str],
                       key: Optional[AgentKey]) -> None:
    """Gate posting into a write-protected room (G1: protect rooms from
    strangers). 'open' admits everyone; 'key' admits the room write-key or
    the room owner's Bearer key."""
    if room.write_policy != "key":
        return
    if provided_room_key and room.write_key_hash and secrets.compare_digest(
        hash_key(provided_room_key), room.write_key_hash
    ):
        return
    if key is not None and room.owner_key_id == key.id:
        return
    raise AuthError(
        "room is write-protected: provide the room write-key "
        "(X-Room-Key header) or the owner's Bearer key",
        status=403,
    )


def subject_of(key: Optional[AgentKey], ip: str) -> str:
    return f"key:{key.id}" if key is not None else f"ip:{ip}"


def daily_quota(key: Optional[AgentKey], kind: str) -> int:
    """Effective daily budget for a subject: per-key override or tier default."""
    idx = 0 if kind == "msg" else 1
    if key is None:
        return TIER_QUOTAS["anon"][idx]
    override = key.daily_msg_quota if kind == "msg" else key.daily_room_quota
    if override is not None:
        return override
    return TIER_QUOTAS.get(key.tier, TIER_QUOTAS["free"])[idx]


def _seconds_to_utc_midnight() -> int:
    now = utcnow()
    return max(1, 86400 - (now.hour * 3600 + now.minute * 60 + now.second))


def check_and_count(session: Session, subject: str, kind: str,
                    key: Optional[AgentKey]) -> None:
    """Increment today's counter for (subject, kind); reject when over budget.

    The increment rides the caller's transaction: on a 4xx raised here (or any
    later handler failure) it rolls back with everything else, so in hard mode
    the stored counter never exceeds the quota.
    """
    quota = daily_quota(key, kind)
    day = utcnow().strftime("%Y-%m-%d")
    conn = session.connection()
    conn.exec_driver_sql(
        "INSERT INTO usage_counters (subject, day, kind, count) VALUES (?, ?, ?, 1) "
        "ON CONFLICT(subject, day, kind) DO UPDATE SET count = count + 1",
        (subject, day, kind),
    )
    count = conn.exec_driver_sql(
        "SELECT count FROM usage_counters WHERE subject = ? AND day = ? AND kind = ?",
        (subject, day, kind),
    ).scalar_one()
    if count > quota:
        if QUOTA_MODE != "hard":
            # Phase 0: meter, log what *would* have been cut, let it through.
            log.info("quota soft-exceeded: %s %s %d/%d", subject, kind, count, quota)
            return
        retry_after = _seconds_to_utc_midnight()
        hint = "" if subject.startswith("key:") else f" Hint: {GET_KEY_HINT}."
        raise QuotaExceeded(
            f"daily {kind} quota reached ({quota}/day for this "
            f"{'key' if subject.startswith('key:') else 'IP'}). "
            f"Resets at UTC midnight.{hint}",
            retry_after=retry_after,
        )
