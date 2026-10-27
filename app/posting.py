"""The write burst of posting a message, shared by all three transports.

REST (`main.post_message`), A2A (`a2a_ops.send`) and MCP (`mcp_server`) all put
a message into a room, and all three did it the same expensive way: COUNT(*)
the room's messages to check the 1000 cap, INSERT, then UPDATE the room to push
its TTL out. The count was an index scan on every post, the TTL write rewrote
an indexed column on every post, and both happened while holding SQLite's
single write lock (profile 05.10.2026).

`claim_slot` replaces the pair with one UPDATE that is also the cap check:

    UPDATE rooms SET messages_count = messages_count + 1 [, expires_at = ?]
     WHERE uuid = ? AND messages_count < cap

Doing it in one statement also makes the cap exact. Read-then-insert let two
concurrent posts both pass a count of 999 and leave the room at 1001; here the
loser's UPDATE matches no row.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlmodel import Session

from . import ttl
from .models import Room

log = logging.getLogger("roomcomm.posting")

#: Messages one room will ever hold. Permanent for the room — not a quota.
MAX_MESSAGES_PER_ROOM = 1000

ROOM_FULL_DETAIL = (
    "room_full: room message limit reached (1000) — permanent for this room, "
    "not your quota. Ask the owner for a new room."
)


def claim_slot(session: Session, room: Room,
               cap: int = MAX_MESSAGES_PER_ROOM) -> bool:
    """Take one of the room's message slots, extending its TTL in the same
    statement. False when the room is full — nothing was written.

    The first write of the posting transaction, so the write lock is taken
    here and released a few statements later at the commit. Everything that
    can reject the post (write policy, protected names, signature check,
    quota) belongs before this call.
    """
    expiry = ttl.next_expiry(room)
    sets = "messages_count = messages_count + 1"
    params: list[object] = []
    if expiry is not None:
        sets += ", expires_at = ?"
        params.append(expiry)
    params.append(room.uuid)
    params.append(cap)
    result = session.connection().exec_driver_sql(
        f"UPDATE rooms SET {sets} WHERE uuid = ? AND messages_count < ?",
        tuple(params),
    )
    if result.rowcount != 1:
        return False
    # The row moved underneath the loaded instance. Expiring just these two
    # columns keeps it honest without the blanket re-SELECT on commit that
    # database.get_session deliberately turns off.
    session.expire(room, ["messages_count", "expires_at"])
    return True


def room_message_count(session: Session, room_uuid: str) -> Optional[int]:
    """The stored counter for a room, or None if the room is gone. For the
    admin views and tests — the posting path never needs to read it."""
    row = session.connection().exec_driver_sql(
        "SELECT messages_count FROM rooms WHERE uuid = ?", (room_uuid,)
    ).fetchone()
    return None if row is None else int(row[0])
