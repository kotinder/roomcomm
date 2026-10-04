"""Inbox — "did anyone look for me?" — the per-key cross-room digest.

One call with a Bearer key replaces polling every room separately: it returns
what's new in every room the key participates in, plus fresh messages in those
rooms that mention the key's agent_id.

The KEY is the boundary, not the name: agent_id is free text anyone may pick,
and a room UUID is its access. So a mention counts only in a room this key
has posted in or read — never in a room it has not joined. (Until 01.10.2026
mentions came from every room, which handed private rooms' UUIDs and text to
any key that chose the right name.)

The read watermark lives in room_seen (see models.RoomSeen) and advances
automatically: reading a room's messages with the Bearer key moves it to the
last id actually returned; posting moves it to the posted message's id. The
inbox itself never advances anything — checking what's new must stay free of
side effects so agents can poll it safely.

Shared by BOTH transports (REST handlers in main.py, MCP tools in
mcp_server.py) — like quota.py, this module must import neither.
"""
import time
from datetime import timedelta
from threading import Lock
from typing import Optional

from sqlmodel import Session, select, func

from . import ttl
from .models import AgentKey, Message, Room, RoomSeen, utcnow

# Substring search for an agent_id shorter than this is all noise ("ai", "gp").
MENTION_MIN_NAME = 3
MENTION_WINDOW_DAYS = 7
MENTION_LIMIT = 20
MENTION_SCAN_LIMIT = 400   # newest candidate rows examined per inbox build
ROOMS_LIMIT = 100
SNIPPET_LEN = 200


def advance_seen(session: Session, key_id: int, room_uuid: str,
                 up_to: Optional[int]) -> None:
    """Move the key's watermark in a room forward to ``up_to`` — never
    backward. Rides the caller's transaction; the caller commits."""
    if not up_to:
        return
    invalidate_awaiting(key_id)  # what you just read no longer awaits you
    row = session.get(RoomSeen, (key_id, room_uuid))
    if row is None:
        session.add(RoomSeen(key_id=key_id, room_uuid=room_uuid,
                             last_seen_msg_id=up_to))
    elif up_to > row.last_seen_msg_id:
        row.last_seen_msg_id = up_to
        row.updated_at = utcnow()
        session.add(row)


def advance_seen_best_effort(session: Session, key: Optional[AgentKey],
                             room_uuid: str, rows: list[Message]) -> None:
    """Advance from a read path and commit. Watermark bookkeeping must never
    fail a read, so every error is swallowed."""
    if key is None or not rows:
        return
    try:
        advance_seen(session, key.id, room_uuid, max(m.id for m in rows))
        session.commit()
    except Exception:
        session.rollback()


def _room_entry(session: Session, key: AgentKey, room: Room,
                baseline: Optional[int]) -> dict:
    """One inbox row for a room. baseline None = no watermark yet → fall back
    to the key's own last message there (pre-watermark participants)."""
    if baseline is None:
        baseline = session.exec(
            select(func.max(Message.id)).where(
                Message.room_uuid == room.uuid, Message.key_id == key.id)
        ).one() or 0
    last = session.exec(
        select(Message).where(Message.room_uuid == room.uuid)
        .order_by(Message.id.desc()).limit(1)
    ).first()
    new_count = session.exec(
        select(func.count()).select_from(Message).where(
            Message.room_uuid == room.uuid, Message.id > baseline)
    ).one()
    return {
        "uuid": room.uuid,
        "description": room.description,
        "new_messages": new_count,
        "last_msg_id": last.id if last else 0,
        "last_from": last.agent_id if last else None,
        "last_at": last.timestamp if last else None,
    }


def build_inbox(session: Session, key: AgentKey) -> dict:
    """The digest as plain data; each transport shapes it into its own output.

    rooms    — every room the key posted in or has a watermark for, newest
               activity first, rooms with news first, capped at ROOMS_LIMIT.
    mentions — messages from the last MENTION_WINDOW_DAYS in those same rooms
               whose text contains the key's agent_id, except the key's own
               posts and anything behind the watermark. Newest first.
    """
    posted = set(session.exec(
        select(Message.room_uuid).where(Message.key_id == key.id).distinct()
    ).all())
    seen = {
        r.room_uuid: r.last_seen_msg_id
        for r in session.exec(select(RoomSeen).where(RoomSeen.key_id == key.id)).all()
    }

    rooms_out = []
    for uid in posted | set(seen):
        room = session.get(Room, uid)
        if room is None:
            continue  # deleted since — stale watermark, not an error
        rooms_out.append(_room_entry(session, key, room, seen.get(uid)))
    rooms_out.sort(
        key=lambda r: (r["new_messages"] > 0, r["last_msg_id"]), reverse=True)
    rooms_out = rooms_out[:ROOMS_LIMIT]

    mentions = _mentions(session, key, posted | set(seen), seen, MENTION_LIMIT)
    return {"agent_id": key.agent_id, "rooms": rooms_out, "mentions": mentions}


def _mentions(session: Session, key: AgentKey, rooms: set[str],
              seen: dict[str, int], limit: int) -> list[dict]:
    """Unread messages of the last MENTION_WINDOW_DAYS in the key's own
    ``rooms`` that name its agent_id, newest first — the key's own posts and
    anything behind its watermark excluded."""
    mentions: list[dict] = []
    name = (key.agent_id or "").strip().lower()
    if len(name) < MENTION_MIN_NAME or not rooms:
        return mentions
    cutoff = utcnow() - timedelta(days=MENTION_WINDOW_DAYS)
    candidates = session.exec(
        select(Message).where(
            Message.room_uuid.in_(rooms),
            func.instr(func.lower(Message.text), name) > 0,
            Message.timestamp >= cutoff,
        ).order_by(Message.id.desc()).limit(MENTION_SCAN_LIMIT)
    ).all()
    for m in candidates:
        if m.key_id == key.id or m.agent_id == key.agent_id:
            continue  # talking about yourself is not a mention
        baseline = seen.get(m.room_uuid)
        if baseline is not None and m.id <= baseline:
            continue  # already read
        mentions.append({
            "room_uuid": m.room_uuid,
            "msg_id": m.id,
            "by": m.agent_id,
            "text": m.text[:SNIPPET_LEN],
            "at": m.timestamp,
        })
        if len(mentions) >= limit:
            break
    return mentions


AWAITING_LIMIT = 3
# "Awaiting you" rides on every keyed read, and agents poll: it must stay
# cheap whatever the key's history. Mentions are looked for among the newest
# AWAITING_SCAN messages only (a primary-key range, not a table scan). The
# digest is cached per key AND per newest message id on the server: any new
# message anywhere refreshes it (a fresh mention is never late), a moved
# watermark drops it (a room you just read never lingers), and an agent
# polling a quiet server pays one primary-key lookup.
AWAITING_SCAN = 5000
AWAITING_TTL = 30.0
_await_cache: dict[tuple[int, str, Optional[str], int], tuple[float, dict]] = {}
_await_lock = Lock()


def invalidate_awaiting(key_id: int) -> None:
    with _await_lock:
        for k in [k for k in _await_cache if k[0] == key_id]:
            del _await_cache[k]


def awaiting(session: Session, key: AgentKey, exclude_room: Optional[str] = None,
             now=None) -> dict:
    """"You are awaited elsewhere" — the cheap digest a transport can attach to
    EVERY answer (the notice rides on the calls the
    agent already makes in its loop, so it reaches agents that never learned
    about check_inbox).

    rooms    — rooms where the key has a watermark and newer messages exist;
    mentions — unread recent messages naming the key's agent_id, in rooms
               the key has a watermark for (posting sets one too). Never
               from a room the key has not joined: see the module docstring.
    Both skip ``exclude_room`` (where the agent is right now) and rooms past
    their TTL — nobody can answer there. Side-effect free and never metered:
    it rides on a request that already was. Cached, see AWAITING_TTL."""
    top = session.exec(select(func.max(Message.id))).one() or 0
    ck = (key.id, (key.key_hash or "")[:16], exclude_room, top)
    t = time.monotonic()
    with _await_lock:
        hit = _await_cache.get(ck)
        if hit is not None and t - hit[0] < AWAITING_TTL:
            return hit[1]
    data = _awaiting_uncached(session, key, exclude_room, now, top)
    with _await_lock:
        if len(_await_cache) > 5000:  # bounded: drop everything, rebuild lazily
            _await_cache.clear()
        _await_cache[ck] = (t, data)
    return data


def _awaiting_uncached(session: Session, key: AgentKey, exclude_room: Optional[str],
                       now=None, top: int = 0) -> dict:
    def _open(room_uuid: str) -> bool:
        if room_uuid == exclude_room:
            return False
        room = session.get(Room, room_uuid)
        return room is not None and not ttl.is_expired(room, now)

    rows = session.exec(
        select(Message.room_uuid, func.count(Message.id))
        .join(RoomSeen, (RoomSeen.room_uuid == Message.room_uuid)
              & (RoomSeen.key_id == key.id))
        .where(Message.id > RoomSeen.last_seen_msg_id)
        .group_by(Message.room_uuid)
        .order_by(func.max(Message.id).desc())
        .limit(AWAITING_LIMIT * 4)
    ).all()
    rooms = [{"uuid": r, "new_messages": n} for r, n in rows if _open(r)][:AWAITING_LIMIT]

    mentions: list[dict] = []
    name = (key.agent_id or "").strip().lower()
    seen = {
        r.room_uuid: r.last_seen_msg_id
        for r in session.exec(select(RoomSeen).where(RoomSeen.key_id == key.id))
    }
    if len(name) >= MENTION_MIN_NAME and seen:
        cutoff = utcnow() - timedelta(days=MENTION_WINDOW_DAYS)
        candidates = session.exec(
            select(Message).where(
                Message.id > top - AWAITING_SCAN,
                Message.room_uuid.in_(list(seen)),
                Message.timestamp >= cutoff,
                func.instr(func.lower(Message.text), name) > 0,
            ).order_by(Message.id.desc()).limit(MENTION_SCAN_LIMIT)
        ).all()
        candidates = [m for m in candidates
                      if m.key_id != key.id and m.agent_id != key.agent_id]
        for m in candidates:
            if m.room_uuid not in seen:
                continue  # belt and braces: never a room this key has not joined
            if m.id <= seen[m.room_uuid] or not _open(m.room_uuid):
                continue
            mentions.append({
                "room_uuid": m.room_uuid,
                "msg_id": m.id,
                "by": m.agent_id,
                "text": m.text[:SNIPPET_LEN],
                "at": m.timestamp,
            })
            if len(mentions) >= AWAITING_LIMIT:
                break
    return {"rooms": rooms, "mentions": mentions}


def is_empty(data: dict) -> bool:
    """True when the inbox has nothing new — callers meter such a poll as an
    idle poll (read_empty), same economics as polling a quiet room."""
    return not data["mentions"] and all(
        r["new_messages"] == 0 for r in data["rooms"])
