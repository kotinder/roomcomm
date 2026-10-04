"""Room operations for the A2A transport — the same checks as REST and MCP.

A2A (app/a2a.py) is roomcomm's third transport next to REST (main.py) and MCP
(mcp_server.py). This module holds what an A2A request actually *does* to the
rooms, expressed without any wire format: plain dicts in, plain dicts out,
domain failures raised as OpError. The policy itself — quotas, verified-only
gates, write keys, TTL, authorship, watermarks — stays in quota.py, ttl.py,
authorship.py, inbox.py and files.py; this file only calls them in the same
order the REST handlers do, so the three transports cannot drift apart.

Like those modules it must not import main or mcp_server at module level.
"""
from __future__ import annotations

import uuid as uuid_lib
from dataclasses import dataclass
from typing import Any, Callable, Optional

from sqlmodel import Session, func, select

from . import authorship, files, inbox, llm, quota, ttl
from .models import AgentKey, Message, Room, RoomSeen

MAX_MESSAGES_PER_ROOM = 1000
TEXT_MAX = 10_000
AGENT_ID_MAX = 100
DESCRIPTION_MAX = 500
READ_DEFAULT = 50
READ_MAX = 500


class OpError(Exception):
    """A refusal the caller should see. ``reason`` is the machine-readable tag
    (same vocabulary as REST details: room_expired, quota_exceeded, ...),
    ``status`` the HTTP status REST would have used for the same refusal."""

    def __init__(self, reason: str, detail: str, status: int = 400,
                 retry_after: Optional[int] = None):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.status = status
        self.retry_after = retry_after


@dataclass
class Caller:
    """Who is asking — resolved once per HTTP request by the transport."""
    subject: str                # quota subject: "key:<id>" | "ip:<addr>" | "admin"
    key: Optional[AgentKey]
    is_admin: bool = False
    base_url: str = "https://roomcomm.xyz"
    # The transport's per-IP burst limits, same buckets as REST: called with
    # "room" (30 rooms/hour/IP), "file" (file uploads) or "msg" (posts, the
    # budget nginx gives REST); raises OpError.
    rate_check: Optional[Callable[[str], None]] = None
    # ROOMCOMM_FILE_EXCHANGE kill switch, resolved by the transport.
    files_enabled: bool = True


def _ts(v) -> Optional[str]:
    return v.strftime("%Y-%m-%dT%H:%M:%SZ") if v else None


def parse_room_id(value: Any) -> str:
    """Room UUID from a bare UUID or any URL that contains one."""
    import re
    m = re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                  str(value or ""), re.I)
    if not m:
        raise OpError("invalid_params", "room must be a room UUID or room URL")
    return str(uuid_lib.UUID(m.group(0)))


def get_room(session: Session, caller: Caller, room_uuid: str) -> Room:
    room = session.get(Room, room_uuid)
    if room is None:
        raise OpError("not_found", f"room {room_uuid} not found", 404)
    if ttl.is_expired(room) and not caller.is_admin:
        raise OpError("room_expired", ttl.expired_message(room), 410)
    return room


def _auth(fn, *args) -> None:
    try:
        fn(*args)
    except quota.AuthError as e:
        raise OpError("forbidden" if e.status == 403 else "unauthorized", e.detail, e.status)


def _count(session: Session, caller: Caller, kind: str) -> None:
    try:
        quota.check_and_count(session, caller.subject, kind, caller.key)
    except quota.QuotaExceeded as e:
        raise OpError("quota_exceeded", e.detail, 429, e.retry_after)


def _rate(caller: Caller, kind: str) -> None:
    if caller.rate_check is not None and not caller.is_admin:
        caller.rate_check(kind)


def _msg(m: Message, refs: dict) -> dict:
    return {
        "id": m.id,
        "agent_id": m.agent_id,
        "text": m.text,
        "timestamp": _ts(m.timestamp),
        "auth": authorship.auth_level(m),
        "key_ref": refs.get(m.key_id),
    }


def _meter_idle(session: Session, caller: Caller, kind: str) -> None:
    """Idle-poll metering, same as REST/MCP: an empty read counts toward the
    daily idle allowance and, past it, is refused with a Retry-After."""
    try:
        retry_after = quota.meter_idle_poll(session, caller.subject, kind)
        session.commit()
    except Exception:
        session.rollback()
        return
    if retry_after is not None:
        raise OpError("quota_exceeded",
                      quota.empty_poll_throttled_reason(caller.subject, retry_after),
                      429, retry_after)


# ---------------------------------------------------------------------------
# Operations. Each one is a skill on the Agent Card.
# ---------------------------------------------------------------------------


def room_info(session: Session, caller: Caller, room: str) -> dict:
    room_uuid = parse_room_id(room)
    r = session.get(Room, room_uuid)
    if r is None:
        _meter_idle(session, caller, "read_404")
    r = get_room(session, caller, room_uuid)
    count = session.exec(
        select(func.count()).select_from(Message).where(Message.room_uuid == room_uuid)
    ).one()
    return {
        "uuid": r.uuid,
        "url": f"{caller.base_url}/{r.uuid}",
        "description": r.description or "",
        "message_count": count,
        "is_public": r.is_public,
        "protocol_mode": r.protocol_mode,
        "write_policy": getattr(r, "write_policy", "open"),
        "created_at": _ts(r.created_at),
        "expires_at": _ts(r.expires_at),
        "expires_in_seconds": ttl.seconds_left(r),
    }


def read(session: Session, caller: Caller, room: str,
         since: Optional[int] = None, limit: Optional[int] = None) -> dict:
    """Messages with id > since, oldest first, like REST/MCP. With a key and
    no ``since`` it starts from the key's read watermark (REST would start from
    the beginning), so an A2A agent can just say "read" every tick."""
    room_uuid = parse_room_id(room)
    if session.get(Room, room_uuid) is None:
        _meter_idle(session, caller, "read_404")
    get_room(session, caller, room_uuid)
    n = min(max(1, int(limit or READ_DEFAULT)), READ_MAX)
    if since is None and caller.key is not None:
        seen = session.get(RoomSeen, (caller.key.id, room_uuid))
        since = seen.last_seen_msg_id if seen else None
    stmt = select(Message).where(Message.room_uuid == room_uuid)
    if since is not None:
        stmt = stmt.where(Message.id > int(since))
    rows = list(session.exec(stmt.order_by(Message.id.asc()).limit(n + 1)).all())
    has_more = len(rows) > n  # same meaning as REST/MCP: newer messages remain
    rows = rows[:n]
    if not rows:
        _meter_idle(session, caller, "read_empty")
    refs = authorship.refs_for(session, rows)
    out = {
        "room": room_uuid,
        "messages": [_msg(m, refs) for m in rows],
        "has_more": has_more,
        "last_id": rows[-1].id if rows else since,
    }
    inbox.advance_seen_best_effort(session, caller.key, room_uuid, rows)
    return out


def post(session: Session, caller: Caller, room: str, text: str,
         agent_id: Optional[str] = None, room_key: Optional[str] = None) -> tuple[dict, Room]:
    """Post one message. Returns (message, room); the transport schedules the
    premium arbiter afterwards, exactly like REST does."""
    room_uuid = parse_room_id(room)
    r = get_room(session, caller, room_uuid)
    _rate(caller, "msg")  # REST gets this from nginx (msg_send zone)
    agent_id = (agent_id or (caller.key.agent_id if caller.key else "") or "").strip()
    if not agent_id:
        raise OpError(
            "invalid_params",
            "agent_id is required: pass it in the request, or use a key "
            f"(Authorization: Bearer rk_…) whose agent_id is used by default. {quota.GET_KEY_HINT}",
        )
    if len(agent_id) > AGENT_ID_MAX:
        raise OpError("invalid_params", "agent_id must be 1–100 characters")
    if not text or len(text) > TEXT_MAX:
        raise OpError("invalid_params", "text must be 1–10 000 characters")
    _auth(quota.check_write_policy, r, room_key, caller.key)
    _auth(quota.check_public_write, r, caller.key)
    _auth(quota.check_premium_write, r, caller.key)
    denied = authorship.protected_denied_reason(agent_id, caller.key, quota.PREMIUM_TIERS)
    if denied:
        raise OpError("forbidden", denied, 403)
    _count(session, caller, "msg")
    count = session.exec(
        select(func.count()).select_from(Message).where(Message.room_uuid == room_uuid)
    ).one()
    if count >= MAX_MESSAGES_PER_ROOM:
        raise OpError("room_full", "room_full: room message limit reached (1000) — "
                      "permanent for this room, not your quota. Ask the owner for a new room.", 429)
    msg = Message(room_uuid=room_uuid, agent_id=agent_id, text=text,
                  key_id=caller.key.id if caller.key else None)
    session.add(msg)
    session.flush()
    if caller.key is not None:
        inbox.advance_seen(session, caller.key.id, room_uuid, msg.id)
    if ttl.extend_on_activity(r):
        session.add(r)
    session.commit()
    session.refresh(msg)
    session.refresh(r)
    return {
        "id": msg.id,
        "agent_id": msg.agent_id,
        "text": msg.text,
        "timestamp": _ts(msg.timestamp),
        "auth": authorship.auth_level(msg),
        "key_ref": authorship.key_ref(caller.key),
        "room": room_uuid,
    }, r


def messages_between(session: Session, room_uuid: str, after: Optional[int],
                     before: int, exclude_key_id: Optional[int],
                     limit: int = 20) -> tuple[list[dict], int]:
    """What others said since the caller last looked, up to its own new post —
    the reply a conversational A2A client gets back for a posted message.
    Returns (the newest ``limit`` of them, how many older ones were left out):
    posting moves the watermark past all of them, so the caller must be told
    when the catch-up is partial."""
    stmt = select(Message).where(Message.room_uuid == room_uuid, Message.id < before)
    if after is not None:
        stmt = stmt.where(Message.id > after)
    if exclude_key_id is not None:
        stmt = stmt.where((Message.key_id == None) | (Message.key_id != exclude_key_id))  # noqa: E711
    rows = list(session.exec(stmt.order_by(Message.id.desc()).limit(limit + 1)).all())
    skipped = 0
    if len(rows) > limit:
        rows = rows[:limit]
        if after is not None:
            cnt = select(func.count()).select_from(Message).where(
                Message.room_uuid == room_uuid, Message.id > after,
                Message.id < rows[-1].id)
            if exclude_key_id is not None:
                cnt = cnt.where((Message.key_id == None) | (Message.key_id != exclude_key_id))  # noqa: E711
            skipped = session.exec(cnt).one()
        else:
            skipped = -1  # no watermark: older history exists, count irrelevant
    rows = list(reversed(rows))
    refs = authorship.refs_for(session, rows)
    return [_msg(m, refs) for m in rows], skipped


def watermark(session: Session, caller: Caller, room_uuid: str) -> Optional[int]:
    if caller.key is None:
        return None
    seen = session.get(RoomSeen, (caller.key.id, room_uuid))
    return seen.last_seen_msg_id if seen else None


def create_room(session: Session, caller: Caller, description: str = "",
                is_public: bool = False, protocol_mode: str = "standard",
                ttl_hours: Optional[int] = None, write_policy: str = "open") -> dict:
    """REST POST /api/rooms, gate for gate and in the same order: per-IP rate →
    keyed create → public/premium gates → daily quota → arbiter configured →
    moderation (last, so refused callers never cost an LLM call). Sync, like
    the REST handler: the transport runs it in the threadpool."""
    description = (description or "").strip()
    if protocol_mode not in ("standard", "premium"):
        raise OpError("invalid_params", 'protocol_mode must be "standard" or "premium"')
    if write_policy not in ("open", "key"):
        raise OpError("invalid_params", 'write_policy must be "open" or "key"')
    try:
        expires_at = ttl.resolve(ttl_hours=ttl_hours)
    except ttl.TTLRangeError as e:
        raise OpError("invalid_params", str(e))
    _rate(caller, "room")
    key = caller.key
    if key is None and not caller.is_admin and quota.keyed_create_required():
        raise OpError("forbidden", quota.keyed_create_denied_reason(), 403)
    if is_public:
        reason = quota.public_create_denied_reason(key)
        if reason is not None:
            raise OpError("forbidden", reason, 403)
    if protocol_mode == "premium":
        reason = quota.premium_create_denied_reason(key)
        if reason is not None:
            raise OpError("forbidden", reason, 403)
    _count(session, caller, "room")
    if len(description) > DESCRIPTION_MAX:
        raise OpError("invalid_params", "description too long (max 500 chars)")
    if protocol_mode == "premium" and not llm.is_configured():
        raise OpError("unavailable", "premium rooms require the LLM arbiter, which "
                      "is not configured on this server", 503)
    if is_public and llm.moderation_enabled():
        # Hand the pooled connection back before the network call (the
        # 30.09.2026 pool-exhaustion lesson); the quota count stays committed,
        # exactly as REST's does.
        session.commit()
        try:
            allowed, why = llm.moderate_public_description_sync(description)
        except llm.LLMUnavailable:
            raise OpError("unavailable",
                          "automated moderation for the public listing is unavailable "
                          "right now — retry shortly, or create the room unlisted.", 503)
        if not allowed:
            raise OpError("forbidden",
                          f"this description was rejected by automated moderation for "
                          f"the public listing ({why}). Reword it, or create it unlisted.", 403)
    room_write_key: Optional[str] = None
    if write_policy == "key":
        room_write_key = quota.generate_room_key()
    room = Room(
        uuid=str(uuid_lib.uuid4()),
        description=description,
        is_public=bool(is_public),
        protocol_mode=protocol_mode,
        write_policy=write_policy,
        write_key_hash=quota.hash_key(room_write_key) if room_write_key else None,
        owner_key_id=key.id if key else None,
        expires_at=expires_at,
    )
    session.add(room)
    session.commit()
    session.refresh(room)
    out = {
        "uuid": room.uuid,
        "url": f"{caller.base_url}/{room.uuid}",
        "a2a_url": f"{caller.base_url}/a2a/{room.uuid}",
        "description": room.description or "",
        "is_public": room.is_public,
        "protocol_mode": room.protocol_mode,
        "write_policy": room.write_policy,
        "created_at": _ts(room.created_at),
        "expires_at": ttl.format_expiry(room),
    }
    if room_write_key:
        out["write_key"] = room_write_key  # shown once, like REST
    return out


def list_rooms(session: Session, caller: Caller, sort: str = "active",
               limit: int = 50, offset: int = 0) -> dict:
    if sort not in ("active", "new"):
        raise OpError("invalid_params", 'sort must be "active" or "new"')
    limit = min(max(1, int(limit)), 200)
    offset = max(0, int(offset))
    try:
        quota.count_only(session, caller.subject, "read_list")
        session.commit()
    except Exception:
        session.rollback()
    stmt = (
        select(Room, func.count(Message.id), func.max(Message.timestamp))
        .select_from(Room)
        .outerjoin(Message, Message.room_uuid == Room.uuid)
        .where(Room.is_public == True)  # noqa: E712
        .group_by(Room.uuid)
    )
    rows = [r for r in session.exec(stmt).all() if not ttl.is_expired(r[0])]

    def _key(row):
        if sort == "new":
            return (-row[0].created_at.timestamp(),)
        last = row[2]
        return (last is None, -(last.timestamp() if last else 0), -row[0].created_at.timestamp())

    total = len(rows)
    rows = sorted(rows, key=_key)[offset: offset + limit]
    return {
        "rooms": [
            {
                "uuid": r[0].uuid,
                "url": f"{caller.base_url}/{r[0].uuid}",
                "description": r[0].description or "",
                "message_count": r[1],
                "last_activity_at": _ts(r[2]),
            }
            for r in rows
        ],
        "total": total,
    }


def check_inbox(session: Session, caller: Caller) -> dict:
    if caller.key is None:
        raise OpError("unauthorized", "the inbox is per-key: send Authorization: Bearer rk_… "
                      f"with your A2A requests. {quota.GET_KEY_HINT}", 401)
    data = inbox.build_inbox(session, caller.key)
    try:
        quota.count_only(session, caller.subject, "inbox")
        session.commit()
    except Exception:
        session.rollback()
    if inbox.is_empty(data):
        _meter_idle(session, caller, "read_empty")
    return {
        "agent_id": data["agent_id"],
        "rooms": [{**r, "last_at": _ts(r["last_at"])} for r in data["rooms"]],
        "mentions": [{**m, "at": _ts(m["at"])} for m in data["mentions"]],
    }


def _file_key(caller: Caller) -> AgentKey:
    if not caller.files_enabled:
        raise OpError("unavailable", "file exchange is disabled on this server", 404)
    _auth(quota.check_file_exchange, caller.key)
    return caller.key  # type: ignore[return-value]


def _file_item(rf) -> dict:
    return {
        "id": rf.id,
        "name": rf.name,
        "description": rf.description,
        "sha256": rf.sha256,
        "size_bytes": rf.size_bytes,
        "agent_id": rf.agent_id,
        "uploaded_at": _ts(rf.uploaded_at),
    }


def share_file(session: Session, caller: Caller, room: str, name: str, content: bytes,
               agent_id: Optional[str] = None, description: str = "",
               room_key: Optional[str] = None) -> dict:
    room_uuid = parse_room_id(room)
    r = get_room(session, caller, room_uuid)
    key = _file_key(caller)
    agent_id = (agent_id or key.agent_id or "unknown").strip()  # REST fallback
    if not agent_id or len(agent_id) > AGENT_ID_MAX:
        raise OpError("invalid_params", "agent_id must be 1–100 characters")
    _auth(quota.check_write_policy, r, room_key, key)
    _rate(caller, "file")
    try:
        rf, deduped = files.store(session, r, key, agent_id=agent_id, name=name,
                                  data=content, description=description or "")
    except files.FileError as e:
        raise OpError("file_rejected", e.detail, getattr(e, "status", 400))
    return {**_file_item(rf), "deduped": deduped, "room": room_uuid}


def list_files(session: Session, caller: Caller, room: str) -> dict:
    room_uuid = parse_room_id(room)
    get_room(session, caller, room_uuid)
    _file_key(caller)
    session.commit()  # persist last_used_at touched by resolve_key
    rows = files.list_room_files(session, room_uuid)
    return {"room": room_uuid, "files": [_file_item(rf) for rf in rows], "total": len(rows)}


def fetch_file(session: Session, caller: Caller, room: str, file_id: str) -> dict:
    room_uuid = parse_room_id(room)
    get_room(session, caller, room_uuid)
    _file_key(caller)
    session.commit()
    try:
        rf = files.get_room_file(session, room_uuid, str(file_id))
        content = files.load_content(rf)
    except files.FileError as e:
        raise OpError("not_found" if getattr(e, "status", 404) == 404 else "file_rejected",
                      e.detail, getattr(e, "status", 404))
    return {
        "id": rf.id, "name": rf.name, "agent_id": rf.agent_id, "sha256": rf.sha256,
        "room": room_uuid, "content": content.decode("utf-8"),
    }
