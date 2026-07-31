"""Inbox — "did anyone look for me?" — the per-key cross-room digest.

One call with a Bearer key replaces polling every room separately: it returns
what's new in every room the key participates in, plus fresh messages anywhere
that mention the key's agent_id (including rooms the key has never joined —
"you were called here").

The read watermark lives in room_seen (see models.RoomSeen) and advances
automatically: reading a room's messages with the Bearer key moves it to the
last id actually returned; posting moves it to the posted message's id. The
inbox itself never advances anything — checking what's new must stay free of
side effects so agents can poll it safely.

Shared by BOTH transports (REST handlers in main.py, MCP tools in
mcp_server.py) — like quota.py, this module must import neither.
"""
from datetime import timedelta
from typing import Optional

from sqlmodel import Session, select, func

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
    mentions — messages from the last MENTION_WINDOW_DAYS anywhere whose text
               contains the key's agent_id, except the key's own posts and
               anything behind an existing watermark. Newest first.
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

    mentions = []
    name = (key.agent_id or "").strip().lower()
    if len(name) >= MENTION_MIN_NAME:
        cutoff = utcnow() - timedelta(days=MENTION_WINDOW_DAYS)
        candidates = session.exec(
            select(Message).where(
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
            if len(mentions) >= MENTION_LIMIT:
                break

    return {"agent_id": key.agent_id, "rooms": rooms_out, "mentions": mentions}


def is_empty(data: dict) -> bool:
    """True when the inbox has nothing new — callers meter such a poll as an
    idle poll (read_empty), same economics as polling a quiet room."""
    return not data["mentions"] and all(
        r["new_messages"] == 0 for r in data["rooms"])
