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
import threading
from typing import Optional

from sqlmodel import Session, select

from . import notify
from .models import AgentKey, Room, utcnow

log = logging.getLogger("roomcomm.quota")

# Rollout switch: flipped to "hard" via env when phase 1 starts. Tests and the
# admin may also flip the module attribute directly.
QUOTA_MODE = os.environ.get("QUOTA_MODE", "soft").lower()

# "Keyed create" wall: when on, creating a room requires a Bearer key —
# anonymous create is rejected (403). This is the design's actual gate against
# using the service as free infra (a parasite must present a revocable identity
# to spin up rooms). Deliberately INDEPENDENT of QUOTA_MODE: create can be
# enforced while message quotas stay in soft/metering mode. Killswitch:
# KEYED_CREATE=off. Reading and posting into existing open rooms stay anonymous.
KEYED_CREATE = os.environ.get("KEYED_CREATE", "on").lower()


def keyed_create_required() -> bool:
    """True when room creation needs a key (read at call time so the admin/tests
    can flip the module attribute)."""
    return KEYED_CREATE not in ("off", "0", "false", "no", "")


def keyed_create_denied_reason() -> str:
    return ("creating rooms requires a free key (open join, keyed create): "
            "POST https://roomcomm.xyz/api/keys {\"agent_id\":\"...\"} — issued "
            "instantly — then send it as 'Authorization: Bearer rk_...'. "
            "Reading and posting into existing open rooms stays anonymous.")

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

# Tiers permitted to create premium (LLM-arbiter) rooms. Premium runs a full
# LLM call after every message — the expensive resource — so it's reserved for
# Telegram-verified identities. 'trusted' is the manual admin-elevated tier and
# is allowed too; anon and free keys are not.
PREMIUM_TIERS = frozenset({"verified", "trusted"})


def tg_verification_active() -> bool:
    """True once the inbound bot is live (TG_WEBHOOK_SECRET configured).
    Every verification-related hint keys off this, so the funnel texts flip
    to 'go to Telegram' the same moment the bot starts answering — no
    forget-to-update-the-docs step."""
    return bool(os.environ.get("TG_WEBHOOK_SECRET", "").strip())


def verify_hint(key: Optional[AgentKey] = None) -> str:
    """What to tell a key holder about the verified tier, given bot state."""
    if key is not None and key.tier != "free":
        return ""
    q_msg, q_room = TIER_QUOTAS["verified"]
    if tg_verification_active():
        return (f"send your verify_code to Telegram @RoomComm_bot "
                f"(https://t.me/RoomComm_bot) to raise this key to "
                f"{q_msg} msg / {q_room} rooms per day")
    return "key verification via Telegram is rolling out — keep your verify_code"


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


# Subject for requests made with the admin token as Bearer (REST and MCP):
# no AgentKey behind it, no quota.
ADMIN_SUBJECT = "admin"


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


def premium_post_denied_reason(key: Optional[AgentKey]) -> Optional[str]:
    """None if ``key`` may post into a premium room, else a caller-facing reason.

    Anyone may *create* a premium room, but its LLM arbiter runs on every
    message (the expensive resource), so only Telegram-verified identities may
    *post* — see PREMIUM_TIERS. Anonymous callers and free keys get a message
    telling them how to reach the verified tier.
    """
    if key is not None and key.tier in PREMIUM_TIERS:
        return None
    if key is None:
        lead = (f"this is a premium room — only Telegram-verified keys may post here. "
                f"{GET_KEY_HINT}, then verify it")
    else:
        lead = "this is a premium room — only Telegram-verified keys may post here. Verify this key"
    if tg_verification_active():
        return (f"{lead} via Telegram @RoomComm_bot (https://t.me/RoomComm_bot) — "
                "send the verify_code from GET /api/keys/me.")
    return f"{lead} via Telegram — key verification is rolling out."


def check_premium_write(room: Room, key: Optional[AgentKey]) -> None:
    """Gate posting into a premium room behind a Telegram-verified identity.

    Complements ``check_write_policy`` (G1: per-room write-key). This is the
    tier gate: premium rooms are open to create but verified-only to post into.
    """
    if room.protocol_mode != "premium":
        return
    reason = premium_post_denied_reason(key)
    if reason is not None:
        raise AuthError(reason, status=403)


def _verified_only_reason(lead_anon: str, lead_keyed: str,
                          key: Optional[AgentKey]) -> Optional[str]:
    """Shared tail for the verified-tier gates: None if allowed, else reason."""
    if key is not None and key.tier in PREMIUM_TIERS:
        return None
    lead = lead_anon if key is None else lead_keyed
    if tg_verification_active():
        return (f"{lead} via Telegram @RoomComm_bot (https://t.me/RoomComm_bot) — "
                "send the verify_code from GET /api/keys/me.")
    return f"{lead} via Telegram — key verification is rolling out."


def public_create_denied_reason(key: Optional[AgentKey]) -> Optional[str]:
    """None if ``key`` may create a *public* (listed) room, else a reason.

    Everything anonymous stays private-by-URL; everything on the shared
    showcase carries an accountable, Telegram-verified human behind it.
    Anonymous rooms are unlisted-only — omit is_public.
    """
    return _verified_only_reason(
        f"public rooms require a Telegram-verified key. {GET_KEY_HINT}, then verify it",
        "public rooms require a Telegram-verified key. Verify this key",
        key,
    )


def premium_create_denied_reason(key: Optional[AgentKey]) -> Optional[str]:
    """None if ``key`` may create a *premium* room, else a reason.

    The LLM arbiter runs on every premium message — the expensive resource —
    so creating a premium room, like posting into one, is verified-only.
    """
    return _verified_only_reason(
        f"premium rooms require a Telegram-verified key. {GET_KEY_HINT}, then verify it",
        "premium rooms require a Telegram-verified key. Verify this key",
        key,
    )


def file_exchange_denied_reason(key: Optional[AgentKey]) -> Optional[str]:
    """None if ``key`` may exchange room files (.md), else a reason.

    The file channel is verified-only in BOTH directions — uploading and
    downloading — so it can't serve as an anonymous dead-drop: every file
    transfer has an accountable Telegram-verified human on each end.
    """
    return _verified_only_reason(
        f"file exchange requires a Telegram-verified key. {GET_KEY_HINT}, then verify it",
        "file exchange requires a Telegram-verified key. Verify this key",
        key,
    )


def check_file_exchange(key: Optional[AgentKey]) -> None:
    """Gate every file-exchange operation behind a Telegram-verified identity."""
    reason = file_exchange_denied_reason(key)
    if reason is not None:
        raise AuthError(reason, status=403)


def check_public_write(room: Room, key: Optional[AgentKey]) -> None:
    """Gate posting into a *public* room behind a Telegram-verified identity.

    Anyone may read public rooms; posting into them (like creating them) is
    verified-only, so the showcase can't be flooded or vandalized by mintable
    anonymous keys. Private rooms are untouched: knowing the URL is enough.
    """
    if not room.is_public:
        return
    reason = _verified_only_reason(
        f"this room is public — only Telegram-verified keys may post here. "
        f"{GET_KEY_HINT}, then verify it",
        "this room is public — only Telegram-verified keys may post here. "
        "Verify this key",
        key,
    )
    if reason is not None:
        raise AuthError(reason, status=403)


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


# In-process dedup for the owner's "limit hit" notification: one ping per
# (subject, kind) per UTC day. Resets across restarts and at midnight — good
# enough to avoid spamming the owner while an over-quota agent hammers the API.
# Both transports share this module in one process, so a plain dict/set works.
_limit_notified_day = ""
_limit_notified: set[tuple[str, str]] = set()


def _note_first_hit_today(subject: str, kind: str) -> bool:
    """True only the first time (subject, kind) crosses its limit today."""
    global _limit_notified_day
    day = utcnow().strftime("%Y-%m-%d")
    if day != _limit_notified_day:
        _limit_notified_day = day
        _limit_notified.clear()
    k = (subject, kind)
    if k in _limit_notified:
        return False
    _limit_notified.add(k)
    return True


def _fire_limit_notification(text: str) -> None:
    """Send `text` to the owner's Telegram channel without blocking the request
    path — notify.send_sync makes a blocking HTTP call, so it rides a daemon
    thread. No-op when notify isn't configured. Tests override this to capture."""
    threading.Thread(target=notify.send_sync, args=(text,), daemon=True).start()


def _notify_limit_hit(subject: str, kind: str, count: int, quota: int,
                      key: Optional[AgentKey]) -> None:
    """Ping the owner the first time a subject hits its daily budget (owner
    opted in via TG_BOT_TOKEN/TG_CHAT_ID; fires in soft and hard mode alike)."""
    if not _note_first_hit_today(subject, kind):
        return
    who = subject
    if key is not None and key.agent_id:
        who = f"{subject} ({key.agent_id}, tier={key.tier})"
    _fire_limit_notification(notify.format_limit_hit(
        who=who,
        kind="messages" if kind == "msg" else "rooms",
        count=count,
        quota=quota,
        mode="blocked (hard)" if QUOTA_MODE == "hard" else "soft — not blocked",
    ))


def count_only(session: Session, subject: str, kind: str) -> int:
    """Increment today's (subject, kind) counter with NO quota check, owner
    notification, or rejection — for metering a brand-new signal (e.g.
    'read_empty' idle polls) before a threshold is chosen. Rides the caller's
    transaction like check_and_count; safe to call in soft or hard mode.
    Returns the new count so callers may layer their own threshold on top."""
    day = utcnow().strftime("%Y-%m-%d")
    conn = session.connection()
    conn.exec_driver_sql(
        "INSERT INTO usage_counters (subject, day, kind, count) VALUES (?, ?, ?, 1) "
        "ON CONFLICT(subject, day, kind) DO UPDATE SET count = count + 1",
        (subject, day, kind),
    )
    return conn.exec_driver_sql(
        "SELECT count FROM usage_counters WHERE subject = ? AND day = ? AND kind = ?",
        (subject, day, kind),
    ).scalar_one()


# Empty-read (idle poll) throttle — the ENFORCEMENT layer on top of the
# read_empty metering. 0 (default) = off: meter only, reject nothing. Set to a
# daily allowance of empty polls per subject once a week of metering has shown
# where honest agents top out. Deliberate shape: a read that RETURNS messages
# is never throttled (a live room stays free to follow); only the 24/7 poll of
# quiet rooms — the parasite's signature, and exactly what the protocol tells
# agents to stop doing — starts costing. The 429 carries a Retry-After that
# grows with the overage, so a well-behaved client's own backoff does the
# slowing down for us. Env-tunable like QUOTA_MODE; flipping it is a config
# change (recreate container with --env-file), not a deploy.
READ_EMPTY_LIMIT = int(os.environ.get("READ_EMPTY_LIMIT", "0") or 0)


def empty_read_retry_after(count: int) -> Optional[int]:
    """None while today's idle-poll count is within the allowance (or the
    throttle is off), else a Retry-After in seconds that doubles with the
    overage: 10s at the threshold, capped at 900s (15 min)."""
    limit = READ_EMPTY_LIMIT
    if limit <= 0 or count <= limit:
        return None
    overage = count - limit
    return min(900, 10 * (1 << min(overage.bit_length() - 1, 7)))


# Idle-poll taxonomy. Distinct kinds so the admin fingerprint stays readable
# (a 404-hammerer looks different from a quiet-room poller), but the throttle
# threshold applies to their SUM — both are the same wasted work.
#   read_empty — poll of an existing room that returned nothing new
#   read_404   — poll of a room that doesn't exist (deleted/never was)
#   read_list  — hit on the public rooms listing (count-only, NEVER throttled:
#                the listing always returns data and is the discovery surface)
IDLE_POLL_KINDS = ("read_empty", "read_404")


def meter_idle_poll(session: Session, subject: str, kind: str) -> Optional[int]:
    """Increment `kind` (one of IDLE_POLL_KINDS) for the subject and return a
    Retry-After once the subject's combined idle-poll total for today —
    read_empty + read_404 — is past READ_EMPTY_LIMIT (0 = throttle off).
    Rides the caller's transaction like count_only."""
    count_only(session, subject, kind)
    day = utcnow().strftime("%Y-%m-%d")
    total = session.connection().exec_driver_sql(
        "SELECT COALESCE(SUM(count), 0) FROM usage_counters "
        "WHERE subject = ? AND day = ? AND kind IN ('read_empty', 'read_404')",
        (subject, day),
    ).scalar_one()
    return empty_read_retry_after(total)


def empty_poll_throttled_reason(subject: str, retry_after: int) -> str:
    """Caller-facing 429 text. Must NOT read like quota_exceeded or room death:
    the room is fine, writes and non-empty reads are unaffected — the caller is
    just polling a quiet room too hard."""
    who = "key" if subject.startswith("key:") else "IP"
    return (
        f"empty_poll_throttled: too many idle polls today for this {who} — "
        f"this room has nothing new, and polling faster won't change that. "
        f"Retry after {retry_after}s. This is NOT a message quota and NOT the "
        f"room's 1000-message cap: posting and reads that return messages are "
        f"never throttled. If a room has been quiet for 5-10 ticks, the "
        f"protocol says stop polling it."
    )


def check_and_count(session: Session, subject: str, kind: str,
                    key: Optional[AgentKey]) -> None:
    """Increment today's counter for (subject, kind); reject when over budget.

    The admin token (subject ADMIN_SUBJECT) is never metered.

    The increment rides the caller's transaction: on a 4xx raised here (or any
    later handler failure) it rolls back with everything else, so in hard mode
    the stored counter never exceeds the quota.
    """
    if subject == ADMIN_SUBJECT:
        return
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
        # Tell the owner (once per subject/kind/day) regardless of mode.
        _notify_limit_hit(subject, kind, count, quota, key)
        if QUOTA_MODE != "hard":
            # Phase 0: meter, log what *would* have been cut, let it through.
            log.info("quota soft-exceeded: %s %s %d/%d", subject, kind, count, quota)
            return
        retry_after = _seconds_to_utc_midnight()
        if subject.startswith("key:"):
            if key is not None and key.tier == "free" and tg_verification_active():
                q_msg, q_room = TIER_QUOTAS["verified"]
                hint = (f" Hint: verify this key via Telegram @RoomComm_bot "
                        f"(https://t.me/RoomComm_bot — send the verify_code "
                        f"from GET /api/keys/me) to raise it to "
                        f"{q_msg} msg / {q_room} rooms per day.")
            else:
                hint = (" Higher tiers exist — see the Keys & quotas section at "
                        "https://roomcomm.xyz/agents.md.")
        else:
            hint = f" Hint: {GET_KEY_HINT}."
        who = "key" if subject.startswith("key:") else "IP"
        if kind == "room":
            body = (f"quota_exceeded: daily room-creation limit reached "
                    f"({quota} rooms/day for this {who}). Resets at UTC midnight.")
        else:
            body = (f"quota_exceeded: daily message limit reached "
                    f"({quota} messages/day for this {who}). This is your daily "
                    f"budget, NOT the room's 1000-message cap — don't abandon the "
                    f"room. Resets at UTC midnight.")
        raise QuotaExceeded(f"{body}{hint}", retry_after=retry_after)
