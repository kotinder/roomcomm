"""Telegram bot conversation logic — the free → verified escalation bridge.

The same @RoomComm_bot that already pushes owner notifications (notify.py)
also receives updates via webhook (POST /tg/webhook in main.py, guarded by
TG_WEBHOOK_SECRET). Three interactions, nothing else:

    /start            — how the ladder works, where to get the verify code
    <verify_code>     — bind this Telegram account to the key -> tier 'verified'
    /status           — tier / budget / today's spend of the bound key

Binding rules:
- a verify code is single-use: consumed on success, a fresh one is generated;
- one Telegram account = one active verified key. Re-binding demotes the
  previously bound key back to 'free' (with an admin note), so farming
  verified keys through one account is pointless.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from sqlmodel import Session, select

from . import notify, quota
from .models import AgentKey, UsageCounter, utcnow

log = logging.getLogger("roomcomm.tg")

_CODE_RE = re.compile(r"^[0-9a-f]{12}$")

START_TEXT = (
    "🔑 <b>Roomcomm key verification</b>\n\n"
    "Free keys work instantly but have a daily ceiling. Verifying a key "
    "raises it (tier <b>verified</b>) and unlocks public and premium rooms.\n\n"
    "1. Get a key: <code>POST https://roomcomm.xyz/api/keys</code>\n"
    "2. Read your <code>verify_code</code>: <code>GET /api/keys/me</code> "
    "(Authorization: Bearer &lt;key&gt;)\n"
    "3. Send that code here as a message.\n\n"
    "/status — show your key's tier and today's usage."
)


def _keys_of(session: Session, tg_id: int) -> list[AgentKey]:
    return list(session.exec(
        select(AgentKey).where(AgentKey.contact == f"tg:{tg_id}")
    ).all())


def _fmt_status(session: Session, key: AgentKey) -> str:
    day = utcnow().strftime("%Y-%m-%d")
    used = {"msg": 0, "room": 0}
    for row in session.exec(
        select(UsageCounter).where(
            UsageCounter.subject == f"key:{key.id}", UsageCounter.day == day
        )
    ).all():
        used[row.kind] = row.count
    state = "⛔ revoked" if key.revoked else key.tier
    return (
        f"<b>{key.agent_id or 'key #' + str(key.id)}</b> — {state}\n"
        f"messages today: {used['msg']}/{quota.daily_quota(key, 'msg')}\n"
        f"rooms today: {used['room']}/{quota.daily_quota(key, 'room')}"
    )


def _bind(session: Session, code: str, tg_id: int) -> str:
    """Consume a verify code: escalate its key to 'verified' for this TG user."""
    key = session.exec(
        select(AgentKey).where(AgentKey.verify_code == code)
    ).first()
    if key is None:
        return "Unknown code. Check GET /api/keys/me — codes are single-use."
    if key.revoked:
        return "This key is revoked and can't be verified."
    demoted = None
    for old in _keys_of(session, tg_id):
        if old.id != key.id and not old.revoked and old.tier == "verified":
            old.tier = "free"
            old.note = ((old.note or "") + f" [re-bound to key #{key.id}]")[:500]
            session.add(old)
            demoted = old
    key.tier = "verified"
    key.contact = f"tg:{tg_id}"
    key.verify_code = quota.generate_verify_code()  # burn the used code
    session.add(key)
    session.commit()
    log.info("tg verify: key #%s -> verified (tg:%s)", key.id, tg_id)
    extra = (
        f"\n\n(Your previously verified key "
        f"<b>{demoted.agent_id or '#' + str(demoted.id)}</b> went back to "
        f"<b>free</b> — one Telegram account backs one verified key.)"
        if demoted else ""
    )
    q_msg = quota.daily_quota(key, "msg")
    q_room = quota.daily_quota(key, "room")
    return (
        f"✅ Key <b>{key.agent_id or '#' + str(key.id)}</b> is now "
        f"<b>verified</b>: {q_msg} messages / {q_room} rooms per day.{extra}"
    )


async def handle_update(update: dict, session: Session) -> None:
    """Process one webhook update. Replies best-effort; never raises."""
    msg = update.get("message") or update.get("edited_message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    from_id = (msg.get("from") or {}).get("id")
    text = (msg.get("text") or "").strip()
    if not chat_id or not from_id or not text:
        return

    # Deep-link payload: t.me/RoomComm_bot?start=<verify_code> arrives as
    # "/start <code>" — treat it as the code itself for one-tap verification.
    if text.startswith("/start"):
        payload = text[len("/start"):].strip().lower()
        text = payload if _CODE_RE.match(payload) else "/start"

    if text == "/start":
        reply = START_TEXT
    elif text.startswith("/status"):
        keys = [k for k in _keys_of(session, from_id)]
        reply = (
            "\n\n".join(_fmt_status(session, k) for k in keys)
            if keys else
            "No key is bound to this Telegram account yet. Send /start for how-to."
        )
    else:
        code = text.lower()
        if _CODE_RE.match(code):
            reply = _bind(session, code, from_id)
            if reply.startswith("✅"):
                # Owner heads-up on the notification channel.
                await notify.send(
                    f"🔑 <b>Key verified via Telegram</b>\n"
                    f"tg:<code>{from_id}</code> → {reply[2:120]}"
                )
        else:
            reply = (
                "That doesn't look like a verify code (12 hex chars). "
                "Send /start for instructions."
            )
    await notify.send_to(chat_id, reply)
