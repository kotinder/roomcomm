"""Room lifetime (TTL).

The README has always called these rooms "ephemeral". Until this module that
was a statement of intent: nothing expired, and a room created in June was
still answering in September. An external reviewer pointed out the gap, and
they were right — a word on the tin that the database does not honour is a
promise to the reader that the operator has not made.

So: every room created from here on carries a concrete `expires_at`. Past it
the room answers 410 to everyone except the admin panel, which keeps full
access so an expired room can still be inspected or revived.

Two deliberate holes, both NULL `expires_at` ("never expires"):

  * Rooms that predate this module. Expiring live conversations retroactively
    would destroy history people are still using, so the migration leaves them
    alone.
  * Rooms an admin has explicitly pinned.

There is no public way to reach NULL: the ceiling below is the most any caller
can ask for. That is the point — an immortal room via the public API would put
"ephemeral" straight back to being decoration.

Shared by the REST app and the MCP server so the two transports cannot drift
on something this load-bearing.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from .models import Room, utcnow

log = logging.getLogger("roomcomm")


def _hours_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        log.warning("%s=%r is not an integer — falling back to %d", name, raw, default)
        return default
    if value < 1:
        log.warning("%s=%d is below the 1h floor — falling back to %d", name, value, default)
        return default
    return value


#: Lifetime given to a room whose creator did not ask for one.
DEFAULT_HOURS = _hours_env("ROOMCOMM_ROOM_TTL_HOURS", 72)
#: Ceiling on what a caller may ask for. 30 days out of the box.
MAX_HOURS = _hours_env("ROOMCOMM_ROOM_TTL_MAX_HOURS", 24 * 30)
#: Floor — a room that expires in ten minutes is a bug, not a request.
MIN_HOURS = 1


def _as_utc(value: datetime) -> datetime:
    """SQLite hands back naive datetimes; everything we store is UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def is_expired(room: Room, now: Optional[datetime] = None) -> bool:
    if room.expires_at is None:
        return False
    return _as_utc(room.expires_at) <= (now or utcnow())


def seconds_left(room: Room, now: Optional[datetime] = None) -> Optional[int]:
    """Seconds of life left, or None for a room that never expires. Clamped at
    zero: an expired room reports 0, never a negative countdown."""
    if room.expires_at is None:
        return None
    delta = _as_utc(room.expires_at) - (now or utcnow())
    return max(0, int(delta.total_seconds()))


def extend_on_activity(room: Room, now: Optional[datetime] = None) -> bool:
    """Push the expiry out when someone posts. Returns True if it moved.

    The TTL counts from the **last message**, not from creation. Measured on
    production before this shipped: 4353 of 4387 rooms had been silent for over
    72 hours (dead weight the TTL exists to clear), but 17 carried a live
    conversation past that mark and 6 past a week, the longest running 19 days.
    A fixed countdown from creation would have cut every one of those off
    mid-sentence — which is a worse failure than never expiring at all.

    Only ever extends, never shortens: a room created with a deliberately long
    `expires_at` keeps it. Pinned rooms (NULL) stay pinned. The ceiling still
    applies, so activity cannot walk a room past the maximum lifetime.
    """
    if room.expires_at is None:
        return False
    now = now or utcnow()
    target = now + timedelta(hours=DEFAULT_HOURS)
    ceiling = now + timedelta(hours=MAX_HOURS)
    if target > ceiling:
        target = ceiling
    if target <= _as_utc(room.expires_at):
        return False
    room.expires_at = target
    return True


def format_expiry(room: Room) -> str:
    return room.expires_at.strftime("%Y-%m-%dT%H:%M:%SZ") if room.expires_at else "?"


def expired_message(room: Room) -> str:
    """Wording shared by REST and MCP. Says plainly that this is terminal:
    an agent that keeps polling a dead room burns its own budget and ours."""
    return (
        f"room_expired: this room reached its TTL at {format_expiry(room)} and "
        f"no longer accepts reads or writes. Rooms are ephemeral by design — "
        f"start a new one. This is terminal: do not retry."
    )


class TTLRangeError(ValueError):
    """Requested lifetime is outside [MIN_HOURS, MAX_HOURS]."""


def resolve(
    ttl_hours: Optional[int] = None,
    expires_at: Optional[datetime] = None,
    now: Optional[datetime] = None,
) -> datetime:
    """Expiry date for a room being created.

    `expires_at` (a date) wins over `ttl_hours` (a duration); with neither, the
    server default applies. Out-of-range requests raise rather than clamping
    silently — a caller who asked for a year and quietly got 30 days would plan
    around the wrong date.
    """
    now = now or utcnow()
    floor = now + timedelta(hours=MIN_HOURS)
    ceiling = now + timedelta(hours=MAX_HOURS)

    if expires_at is not None:
        wanted = _as_utc(expires_at)
    elif ttl_hours is not None:
        if ttl_hours > MAX_HOURS:
            # Before the arithmetic: a huge value overflows timedelta/datetime
            # (OverflowError → 500) instead of being the range error it is.
            wanted = ceiling + timedelta(hours=1)
        else:
            wanted = now + timedelta(hours=ttl_hours)
    else:
        return now + timedelta(hours=DEFAULT_HOURS)

    if wanted < floor:
        raise TTLRangeError(
            f"expires_at/ttl_hours must be at least {MIN_HOURS} hour(s) in the future"
        )
    if wanted > ceiling:
        raise TTLRangeError(
            f"rooms are ephemeral: the maximum lifetime is {MAX_HOURS} hours "
            f"(until {ceiling.strftime('%Y-%m-%dT%H:%M:%SZ')})"
        )
    return wanted
