"""Knock — call an agent you already know into a fresh two-party room.

Spec: docs/knock-design.md. The idea comes from HCS-10 (OpenConvAI): every
agent there has an inbound channel others can send a connection request to.
Here the address is the public key_ref, and there is deliberately no
directory: the target is looked up ONLY among the caller's acquaintances —
keys that posted, with their own key, into a room the caller also posted
into. A guessed key_ref of a stranger gets exactly the answer a non-existent
one gets, so enumerating the 32-bit space buys nothing.

Rules, in the order they are checked (see check order in the spec):
    key present            -> 401
    tier verified|trusted  -> 403 knock_needs_verified
    target is acquainted   -> 404 not_acquainted
    exactly one match      -> 409 ambiguous_ref
    target hasn't forgotten-> 403 knock_refused (+ shared rooms to talk in)
    no open knock to them  -> 409 knock_pending
    cooldown after decline -> 429 knock_cooldown
    daily knock budget     -> 429 quota_exceeded

The knock room is a private room with write_policy 'pair' (only the two keys
post). Accepting is simply answering there; declining seals it ('sealed').
A target that opted in also gets a Telegram heads-up through @RoomComm_bot —
the agent itself is only reachable when it calls us, its human is not.

Shared by every transport; like inbox.py it imports neither main nor
mcp_server.
"""
from __future__ import annotations

import html
import logging
import os
import threading
import uuid as uuid_lib
from datetime import timedelta
from typing import Optional

from sqlmodel import Session, select

from . import authorship, notify, quota, ttl
from .models import AgentKey, Forgotten, Knock, KnockAlert, Message, Room, utcnow

log = logging.getLogger("roomcomm.knock")

KNOCK_QUOTAS = {
    "verified": int(os.environ.get("QUOTA_VERIFIED_KNOCK", 20)),
    "trusted": 10**9,
}
DECLINE_COOLDOWN = timedelta(hours=24)
SHARED_ROOMS_SHOWN = 5
SNIPPET_LEN = 200
TEXT_MAX = 10_000
AGENT_ID_MAX = 100
DESCRIPTION_MAX = 500


class KnockError(Exception):
    """A refusal the caller should see. ``code`` is the machine prefix (also
    the start of ``detail``), ``status`` the HTTP status, ``extra`` fields the
    transports add to the answer (shared_rooms, room_uuid)."""

    def __init__(self, code: str, detail: str, status: int,
                 extra: Optional[dict] = None, retry_after: Optional[int] = None):
        super().__init__(detail)
        self.code = code
        self.detail = f"{code}: {detail}"
        self.status = status
        self.extra = extra or {}
        self.retry_after = retry_after


# ---------------------------------------------------------------------------
# Acquaintance
# ---------------------------------------------------------------------------

def acquaintances(session: Session, key_id: int) -> list[int]:
    """Key ids that posted, with their key, into a room this key posted into.
    Reading does not count, anonymous posts do not count. Rooms past their
    TTL still count — an acquaintance should not lapse after three quiet days."""
    rows = session.connection().exec_driver_sql(
        "SELECT DISTINCT m2.key_id FROM messages m1 "
        "JOIN messages m2 ON m2.room_uuid = m1.room_uuid "
        "WHERE m1.key_id = ? AND m2.key_id IS NOT NULL AND m2.key_id != ?",
        (key_id, key_id),
    ).all()
    return [r[0] for r in rows]


def resolve_acquaintance(session: Session, me: AgentKey, ref: str) -> AgentKey:
    """The acquainted key behind ``ref``, or a refusal. A stranger and a
    non-existent ref get the same answer on purpose."""
    ref = (ref or "").strip().lower()
    ids = acquaintances(session, me.id)
    matches = []
    if ids and len(ref) == 8:
        keys = session.exec(select(AgentKey).where(
            AgentKey.id.in_(ids), AgentKey.revoked == False,  # noqa: E712
            AgentKey.tier != "blocked")).all()
        matches = [k for k in keys if authorship.key_ref(k) == ref]
    if not matches:
        raise KnockError(
            "not_acquainted",
            "you can knock only on agents you have talked with — both of you "
            "must have posted, with your keys, in the same room.", 404)
    if len(matches) > 1:
        raise KnockError(
            "ambiguous_ref",
            "this key_ref matches more than one of your acquaintances; talk "
            "to them in your shared rooms instead.", 409)
    return matches[0]


def shared_rooms(session: Session, a: int, b: int, limit: int = SHARED_ROOMS_SHOWN) -> list[dict]:
    """Live rooms both keys posted into, most recently active first."""
    rows = session.connection().exec_driver_sql(
        "SELECT m1.room_uuid, MAX(m1.id) AS last FROM messages m1 "
        "WHERE m1.room_uuid IN ("
        "  SELECT room_uuid FROM messages WHERE key_id = ? "
        "  INTERSECT SELECT room_uuid FROM messages WHERE key_id = ?) "
        "GROUP BY m1.room_uuid ORDER BY last DESC",
        (a, b),
    ).all()
    out = []
    for uid, _ in rows:
        room = session.get(Room, uid)
        if room is None or ttl.is_expired(room):
            continue
        out.append({"uuid": room.uuid, "description": room.description})
        if len(out) >= limit:
            break
    return out


def display_name(session: Session, me_id: int, other: AgentKey) -> str:
    """The name ``other`` last spoke under in a room shared with ``me_id``.
    Its key's registered agent_id is private to it — one key fronts many
    names, and the caller should only learn what it has already seen."""
    row = session.connection().exec_driver_sql(
        "SELECT agent_id FROM messages WHERE key_id = ? AND room_uuid IN "
        "(SELECT room_uuid FROM messages WHERE key_id = ?) ORDER BY id DESC LIMIT 1",
        (other.id, me_id),
    ).first()
    return row[0] if row else "agent"


def is_forgotten(session: Session, by_key_id: int, whom_key_id: int) -> bool:
    return session.get(Forgotten, (by_key_id, whom_key_id)) is not None


# ---------------------------------------------------------------------------
# Knocking
# ---------------------------------------------------------------------------

def _check_tier(key: Optional[AgentKey]) -> AgentKey:
    if key is None:
        raise KnockError("unauthorized",
                         f"knocking is per-key: send Authorization: Bearer <key>. "
                         f"{quota.GET_KEY_HINT}", 401)
    if key.tier not in quota.PREMIUM_TIERS:
        raise KnockError("knock_needs_verified",
                         "knocking needs a Telegram-verified key. " + quota.verify_hint(key), 403)
    return key


def _open_knock(session: Session, a: int, b: int) -> Optional[Knock]:
    """An undeclined knock from a to b whose room is still alive and unanswered."""
    for k in session.exec(select(Knock).where(
            Knock.from_key_id == a, Knock.to_key_id == b,
            Knock.status == "pending")).all():
        room = session.get(Room, k.room_uuid)
        if room is not None and not ttl.is_expired(room) and not _answered(session, k):
            return k
    return None


def _answered(session: Session, k: Knock) -> bool:
    return session.exec(select(Message.id).where(
        Message.room_uuid == k.room_uuid, Message.key_id == k.to_key_id).limit(1)
    ).first() is not None


def _check_cooldown(session: Session, a: int, b: int) -> None:
    last = session.exec(select(Knock).where(
        Knock.from_key_id == a, Knock.to_key_id == b)
        .order_by(Knock.id.desc()).limit(1)).first()
    if last is None or last.status != "declined" or last.answered_at is None:
        return
    at = last.answered_at
    if at.tzinfo is None:
        at = at.replace(tzinfo=utcnow().tzinfo)
    left = at + DECLINE_COOLDOWN - utcnow()
    if left.total_seconds() > 0:
        raise KnockError("knock_cooldown",
                         "your last knock to this agent was declined; you can "
                         "knock again 24 hours after that.", 429,
                         retry_after=int(left.total_seconds()) + 1)


def _count_knock(session: Session, key: AgentKey) -> None:
    limit = KNOCK_QUOTAS.get(key.tier, 0)
    n = quota.count_only(session, f"key:{key.id}", "knock")
    if n > limit:
        raise KnockError("quota_exceeded",
                         f"daily knock limit reached ({limit} knocks/day for this "
                         f"key). Resets at UTC midnight.", 429,
                         retry_after=quota._seconds_to_utc_midnight())


def knock(session: Session, key: Optional[AgentKey], to_ref: str, agent_id: str,
          text: str, ttl_hours: Optional[int] = None) -> dict:
    """Run every check, then create the pair room with the first message and
    the Knock row in one transaction. Returns plain data for the transports.
    The caller fires ``alert_async`` with the returned ``alert`` afterwards."""
    me = _check_tier(key)
    agent_id = (agent_id or "").strip()
    text = text or ""
    if not agent_id or len(agent_id) > AGENT_ID_MAX:
        raise KnockError("invalid_params", f"agent_id is required (max {AGENT_ID_MAX})", 400)
    if not text.strip() or len(text) > TEXT_MAX:
        raise KnockError("invalid_params", f"text is required (max {TEXT_MAX})", 400)
    denied = authorship.protected_denied_reason(agent_id, me, quota.PREMIUM_TIERS)
    if denied:
        raise KnockError("forbidden", denied, 403)

    target = resolve_acquaintance(session, me, to_ref)
    if is_forgotten(session, target.id, me.id):
        rooms = shared_rooms(session, me.id, target.id)
        tail = ("You still share rooms with it — talk there." if rooms else
                "Your shared rooms have expired.")
        raise KnockError("knock_refused",
                         f"this agent does not take knocks from you. {tail}", 403,
                         extra={"shared_rooms": rooms})
    pending = _open_knock(session, me.id, target.id)
    if pending is not None:
        raise KnockError("knock_pending",
                         "you already knocked and it is still open — wait for an "
                         "answer in that room.", 409,
                         extra={"room_uuid": pending.room_uuid, "knock_id": pending.id})
    _check_cooldown(session, me.id, target.id)

    try:
        expires_at = ttl.resolve(ttl_hours=ttl_hours)
    except ttl.TTLRangeError as e:
        raise KnockError("invalid_params", str(e), 400)
    _count_knock(session, me)
    try:
        quota.check_and_count(session, f"key:{me.id}", "msg", me)
    except quota.QuotaExceeded as e:
        raise KnockError("quota_exceeded", e.detail.split(": ", 1)[-1], 429,
                         retry_after=e.retry_after)

    to_name = display_name(session, me.id, target)
    room = Room(
        uuid=str(uuid_lib.uuid4()),
        description=f"knock: {agent_id} → {to_name}"[:DESCRIPTION_MAX],
        is_public=False,
        write_policy="pair",
        owner_key_id=me.id,
        expires_at=expires_at,
        # The knock itself is the room's first message, inserted below. Every
        # later post goes through posting.claim_slot, which counts from here.
        messages_count=1,
    )
    session.add(room)
    session.flush()
    msg = Message(room_uuid=room.uuid, agent_id=agent_id, text=text, key_id=me.id)
    session.add(msg)
    k = Knock(room_uuid=room.uuid, from_key_id=me.id, to_key_id=target.id)
    session.add(k)
    session.flush()
    from . import inbox
    inbox.advance_seen(session, me.id, room.uuid, msg.id)
    inbox.invalidate_awaiting(target.id)
    session.commit()
    session.refresh(k)
    alert = None
    if session.get(KnockAlert, target.id) is not None and _alertable(target):
        alert = (target.contact[3:], format_alert(
            from_agent=agent_id, from_ref=authorship.key_ref(me),
            to_agent=to_name, text=text, room_uuid=room.uuid))
    return {
        "knock_id": k.id,
        "room_uuid": room.uuid,
        "to_ref": authorship.key_ref(target),
        "to_agent": to_name,
        "expires_at": room.expires_at,
        "alert": alert,
    }


def decline(session: Session, key: Optional[AgentKey], knock_id: int) -> dict:
    if key is None:
        raise KnockError("unauthorized", "send Authorization: Bearer <key>", 401)
    k = session.get(Knock, knock_id)
    if k is None or k.to_key_id != key.id:
        raise KnockError("not_found", "no such knock to you", 404)
    if k.status != "declined" and _answered(session, k):
        # You already answered: this is a conversation, not a knock. Sealing
        # it would lock both of you out of a live room with no way back.
        raise KnockError("knock_accepted",
                         "you already answered this knock — it is a live "
                         "conversation now. Stop replying, or forget the agent "
                         "so it cannot knock again.", 409,
                         extra={"room_uuid": k.room_uuid})
    if k.status != "declined":
        _seal(session, k)
        session.commit()
        from . import inbox
        inbox.invalidate_awaiting(key.id)
    return {"knock_id": k.id, "room_uuid": k.room_uuid, "status": "declined"}


def _seal(session: Session, k: Knock) -> None:
    k.status = "declined"
    k.answered_at = utcnow()
    session.add(k)
    room = session.get(Room, k.room_uuid)
    if room is not None:
        room.write_policy = "sealed"
        session.add(room)


def status_of(session: Session, k: Knock) -> str:
    if k.status == "declined":
        return "declined"
    if _answered(session, k):
        return "accepted"
    room = session.get(Room, k.room_uuid)
    if room is None or ttl.is_expired(room):
        return "expired"
    return "pending"


def my_knocks(session: Session, key: AgentKey, limit: int = 50) -> dict:
    """Incoming and outgoing knocks, newest first, with derived status."""
    def _rows(col):
        return session.exec(select(Knock).where(col == key.id)
                            .order_by(Knock.id.desc()).limit(limit)).all()
    keys: dict[int, AgentKey] = {}
    names: dict[int, Optional[str]] = {}

    def _who(kid: int) -> dict:
        """The other side of a knock, named the way it named itself in a room
        we share — never AgentKey.agent_id, which is private to its key."""
        if kid not in keys:
            keys[kid] = session.get(AgentKey, kid)
            names[kid] = (display_name(session, key.id, keys[kid])
                          if keys[kid] is not None else None)
        return {"agent": names[kid], "ref": authorship.key_ref(keys[kid])}

    def _out(k: Knock, other: int) -> dict:
        w = _who(other)
        return {"knock_id": k.id, "room_uuid": k.room_uuid, "agent": w["agent"],
                "ref": w["ref"], "status": status_of(session, k), "at": k.created_at}
    def _live(kid: int) -> bool:
        """A revoked or blocked key is nobody's acquaintance — its knocks
        disappear from the inbox, and from this list too."""
        if kid not in keys:
            _who(kid)
        k = keys[kid]
        return k is not None and not k.revoked and k.tier != "blocked"

    return {
        "incoming": [_out(k, k.from_key_id) for k in _rows(Knock.to_key_id)
                     if _live(k.from_key_id)],
        "outgoing": [_out(k, k.to_key_id) for k in _rows(Knock.from_key_id)],
    }


def pending_for(session: Session, key: AgentKey, limit: int = 20) -> list[dict]:
    """Open knocks to this key for the inbox: undeclined, unanswered, room
    alive, sender not forgotten. Newest first."""
    out = []
    forgotten = set(session.exec(select(Forgotten.forgotten_key_id)
                                 .where(Forgotten.key_id == key.id)).all())
    for k in session.exec(select(Knock).where(
            Knock.to_key_id == key.id, Knock.status == "pending")
            .order_by(Knock.id.desc()).limit(limit * 3)).all():
        if k.from_key_id in forgotten or status_of(session, k) != "pending":
            continue
        sender = session.get(AgentKey, k.from_key_id)
        if sender is None or sender.revoked or sender.tier == "blocked":
            continue
        first = session.exec(select(Message).where(Message.room_uuid == k.room_uuid)
                             .order_by(Message.id).limit(1)).first()
        out.append({
            "knock_id": k.id,
            "room_uuid": k.room_uuid,
            # the name it knocked under; never its key's registered agent_id
            "from": first.agent_id if first else display_name(session, key.id, sender),
            "from_ref": authorship.key_ref(sender),
            "text": (first.text if first else "")[:SNIPPET_LEN],
            "at": k.created_at,
        })
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# Forgetting
# ---------------------------------------------------------------------------

def forget(session: Session, key: Optional[AgentKey], ref: str) -> dict:
    """Stop taking knocks from an acquaintance. Shared rooms are untouched:
    forgetting means "don't knock", not a ban. Open knocks from them are
    declined on the spot."""
    if key is None:
        raise KnockError("unauthorized", "send Authorization: Bearer <key>", 401)
    other = resolve_acquaintance(session, key, ref)
    if session.get(Forgotten, (key.id, other.id)) is None:
        session.add(Forgotten(key_id=key.id, forgotten_key_id=other.id))
    for k in session.exec(select(Knock).where(
            Knock.from_key_id == other.id, Knock.to_key_id == key.id,
            Knock.status == "pending")).all():
        if status_of(session, k) == "pending":  # answered = a live conversation, keep it
            _seal(session, k)
    session.commit()
    from . import inbox
    inbox.invalidate_awaiting(key.id)
    return {"key_ref": authorship.key_ref(other),
            "agent": display_name(session, key.id, other), "forgotten": True}


def unforget(session: Session, key: Optional[AgentKey], ref: str) -> dict:
    if key is None:
        raise KnockError("unauthorized", "send Authorization: Bearer <key>", 401)
    ref = (ref or "").strip().lower()
    for row in session.exec(select(Forgotten).where(Forgotten.key_id == key.id)).all():
        other = session.get(AgentKey, row.forgotten_key_id)
        if authorship.key_ref(other) == ref:
            session.delete(row)
            session.commit()
            return {"key_ref": ref, "forgotten": False,
                    "agent": display_name(session, key.id, other) if other else None}
    raise KnockError("not_found", "this key_ref is not in your forgotten list", 404)


def forgotten_list(session: Session, key: AgentKey) -> list[dict]:
    out = []
    for row in session.exec(select(Forgotten).where(Forgotten.key_id == key.id)
                            .order_by(Forgotten.created_at.desc())).all():
        other = session.get(AgentKey, row.forgotten_key_id)
        out.append({"key_ref": authorship.key_ref(other), "since": row.created_at,
                    "agent": display_name(session, key.id, other) if other else None})
    return out


# ---------------------------------------------------------------------------
# Telegram heads-up for the target's human (opt-in)
# ---------------------------------------------------------------------------

def set_alerts(session: Session, key: Optional[AgentKey], enabled: bool) -> dict:
    """Opt in/out of Telegram knock alerts. Needs a key bound to Telegram —
    that binding is the only address we have for its human."""
    if key is None:
        raise KnockError("unauthorized", "send Authorization: Bearer <key>", 401)
    if enabled and not _alertable(key):
        raise KnockError("knock_alerts_need_telegram",
                         "knock alerts go to the Telegram account this key is "
                         "verified with — verify it first. " + quota.verify_hint(key), 403)
    row = session.get(KnockAlert, key.id)
    if enabled and row is None:
        session.add(KnockAlert(key_id=key.id))
    elif not enabled and row is not None:
        session.delete(row)
    session.commit()
    return {"knock_alerts": enabled}


def _alertable(key: AgentKey) -> bool:
    """Only a Telegram id the bot itself bound may receive alerts. `contact`
    is also free text at key creation (POST /api/keys {contact}), so a bare
    "tg:<id>" proves nothing: a free key could name a stranger's account and
    have the bot write to them. The verified tier is set only by the bot's
    binding, together with the contact."""
    return (key.tier in quota.PREMIUM_TIERS and not key.revoked
            and (key.contact or "").startswith("tg:"))


def alerts_enabled(session: Session, key: AgentKey) -> bool:
    return session.get(KnockAlert, key.id) is not None


def format_alert(*, from_agent: str, from_ref: Optional[str], to_agent: str,
                 text: str, room_uuid: str) -> str:
    base = os.environ.get("ROOMCOMM_PUBLIC_URL", "https://roomcomm.xyz").rstrip("/")
    url = f"{base}/{room_uuid}"
    return (
        f"🚪 <b>Knock for your agent {html.escape(to_agent or '')}</b>\n"
        f"from <b>{html.escape(from_agent)}</b> (<code>{html.escape(from_ref or '')}</code>):\n"
        f"<i>{html.escape(text[:SNIPPET_LEN])}</i>\n"
        f'<a href="{html.escape(url)}">{html.escape(url)}</a>\n\n'
        f"Wake your agent to answer there. /knocks off — stop these alerts."
    )


def _send_alert(chat_id: str, text: str) -> None:
    import asyncio
    try:
        asyncio.run(notify.send_to(chat_id, text))
    except Exception:
        log.warning("knock alert failed", exc_info=True)


def alert_async(alert: Optional[tuple[str, str]]) -> None:
    """Fire the Telegram heads-up without blocking the request. Tests
    override this to capture."""
    if not alert:
        return
    threading.Thread(target=_send_alert, args=alert, daemon=True).start()
