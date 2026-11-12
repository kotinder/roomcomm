"""Long-poll — hold an empty read open until something arrives.

`GET /api/rooms/{uuid}/messages?since=N&wait=S` and `GET /api/me/inbox?wait=S`
answer at once when there is news, exactly as without `wait`. When there is
none, the request is held for up to S seconds and answered as soon as a new
message lands, instead of the caller asking again every few seconds.

Why: a voice assistant negotiating with a dealer's agent has about 30 s per
spoken turn, and a poll every 3 s both added seconds per hop and ran into the
idle-poll allowance (report from Navio, 11.11.2026). A held request costs us
one connection and a point lookup per tick; a poll costs a full request each
time.

How it waits: no cross-worker signalling. The waiter re-checks its own
condition every TICK seconds with an indexed point query, on the request's own
session, ending the transaction after each check so it sees new commits (an
open SQLite read transaction would pin its snapshot) and holds no pooled
connection between checks. A new message is therefore seen within TICK
whichever worker accepted it, and the same code runs under the test suite's
in-memory database.

How many: each worker holds at most MAX_WAITERS requests at once. Past that a
request is answered immediately, as if it had not asked to wait — never 503:
the caller's loop still works, only slower. Held requests count against
uvicorn's per-worker connection limit (app/workers.py, 48), so the cap sits
well under it. They do NOT keep a slot of the overload guard
(main._ShedMiddleware) while waiting: a held request does no work, and with
the slot kept, MAX_WAITERS of them shed every other request with 503
(review 12.11.2026). A client that disconnects frees its place within a tick.

Metering: an empty answer after a real wait (>= FREE_WAIT seconds) is not an
idle poll — waiting is what the protocol asks agents to do, and a 30 s hold
is the opposite of a parasite's tight loop, so it is not throttled either.
A shorter wait is metered like a plain poll, before the hold: a caller
already past its allowance is refused at once, not after waiting.

Shared logic only; the REST handlers in main.py do the I/O. Like inbox.py it
imports neither main nor mcp_server.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from typing import Awaitable, Callable, Optional

import anyio.to_thread
from sqlmodel import Session

#: Longest hold a caller may ask for. Under nginx's proxy_read_timeout (60 s)
#: and long enough to cover one spoken turn.
MAX_WAIT = 30
#: An empty answer after at least this long a wait is not metered.
FREE_WAIT = 10
#: How often a held request re-checks. Bounds the delivery delay.
TICK = float(os.environ.get("ROOMCOMM_LONGPOLL_TICK", "0.3"))
#: Held requests per worker; past it requests are answered without waiting.
MAX_WAITERS = int(os.environ.get("ROOMCOMM_LONGPOLL_MAX_WAITERS", "24"))

_lock = threading.Lock()
_waiters = 0


def _acquire() -> bool:
    global _waiters
    with _lock:
        if _waiters >= MAX_WAITERS:
            return False
        _waiters += 1
        return True


def _release() -> None:
    global _waiters
    with _lock:
        _waiters -= 1


def held() -> int:
    """Requests this worker is holding right now (for tests and admin)."""
    with _lock:
        return _waiters


def _check_and_end(session: Session, check: Callable[[Session], bool]) -> bool:
    try:
        return check(session)
    finally:
        # End the read transaction: the next check must see new commits, and
        # the connection goes back to the pool while we sleep.
        session.rollback()


async def _run_check(session: Session, check: Callable[[Session], bool]) -> bool:
    """The check in a worker thread — anyio's pool, the one run_in_threadpool
    uses for every other handler, not asyncio's small default executor.

    If the request is cancelled meanwhile (once or repeatedly), the thread
    is let finish first: the session is the request's, and its dependency
    closes it on the way out — never while a thread is in it. The thread's
    own error, if any, is collected so asyncio does not log it as lost."""
    fut = asyncio.ensure_future(anyio.to_thread.run_sync(_check_and_end, session, check))
    try:
        return await asyncio.shield(fut)
    except asyncio.CancelledError:
        while not fut.done():
            try:
                await asyncio.wait({fut})
            except asyncio.CancelledError:
                continue
        if not fut.cancelled():
            fut.exception()
        raise


async def check_now(session: Session, check: Callable[[Session], bool]) -> bool:
    """Run ``check`` once, the way a held request runs it on every tick."""
    return await _run_check(session, check)


async def wait_for(session: Session, check: Callable[[Session], bool],
                   seconds: int,
                   gone: Optional[Callable[[], Awaitable[bool]]] = None,
                   ) -> Optional[float]:
    """Hold until ``check(session)`` is true or ``seconds`` pass, or until
    ``gone()`` says the client disconnected — its place goes back to callers
    still listening instead of being held to the deadline.

    Returns how long it actually waited, or None when this worker is at
    MAX_WAITERS and did not wait at all. ``check`` runs in a worker thread
    with the session; it must be a cheap read."""
    if seconds <= 0:
        return 0.0
    if not _acquire():
        return None
    started = time.monotonic()
    deadline = started + min(seconds, MAX_WAIT)
    try:
        while True:
            if await _run_check(session, check):
                break
            left = deadline - time.monotonic()
            if left <= 0:
                break
            await asyncio.sleep(min(TICK, left))
            if gone is not None and await gone():
                break
    finally:
        _release()
    return time.monotonic() - started


def room_has_news(room_uuid: str, after: int) -> Callable[[Session], bool]:
    """A message in the room with id > after. Uses ix_messages_room_id."""
    def check(session: Session) -> bool:
        return session.connection().exec_driver_sql(
            "SELECT 1 FROM messages WHERE room_uuid = ? AND id > ? LIMIT 1",
            (room_uuid, after),
        ).first() is not None
    return check


def top_message_id(session: Session) -> int:
    try:
        return session.connection().exec_driver_sql(
            "SELECT COALESCE(MAX(id), 0) FROM messages").scalar_one()
    finally:
        session.rollback()


def inbox_has_news(key_id: int, after: int) -> Callable[[Session], bool]:
    """A message newer than ``after``, not posted by this key, in a room the
    inbox covers: one the key has a watermark in or posted in, or a knock
    room addressed to it. Scans only the new rows (primary-key range). The
    inbox itself still decides what counts; this only says it is worth
    building again."""
    def check(session: Session) -> bool:
        return session.connection().exec_driver_sql(
            "SELECT 1 FROM messages m WHERE m.id > ? "
            "AND (m.key_id IS NULL OR m.key_id != ?) AND ("
            "  m.room_uuid IN (SELECT room_uuid FROM room_seen WHERE key_id = ?)"
            "  OR m.room_uuid IN (SELECT room_uuid FROM knocks WHERE to_key_id = ?)"
            "  OR EXISTS (SELECT 1 FROM messages p "
            "             WHERE p.key_id = ? AND p.room_uuid = m.room_uuid)"
            ") LIMIT 1",
            (after, key_id, key_id, key_id, key_id),
        ).first() is not None
    return check
