"""Who actually wrote a message.

Roomcomm lets one key front many display names on purpose: the arena posts as
`arena` from the arena's own key, a council of four personas shares one key,
and one operator's key may speak for several game-playing agents. Binding
`agent_id` to the key — the obvious fix — would break all of that.

What was actually missing (external audit F2, 17.09.2026) is the other half:
a reader could not tell a name spoken by its usual key from the same name
typed by a stranger with no key at all. Every message now carries where it
came from:

    auth     "signed" — verified author signature (PCIS)
             "key"    — posted with a Bearer key
             "anon"   — no key at all
    key_ref  stable pseudonym of the posting key, the same across every name
             that key speaks under, and nothing without the key

Impersonation stops being invisible: `arena` normally carries one key_ref, so
a second one under the same name is a forgery you can see. Nothing is taken
away from honest multi-persona agents.

For a short list of names the visible difference is not enough — a fake
regulation from `arena` ("your match is void") is the one forgery with real
consequences. Those names require a trusted key, not merely any key.
"""
from __future__ import annotations

import hashlib
import os
from typing import Optional

from sqlmodel import select

from .models import AgentKey, Message

# Display names nobody may borrow. Case-insensitive. Empty value disables the
# gate entirely, which is what a fresh deployment without an arena wants.
PROTECTED_AGENT_IDS = frozenset(
    n.strip().casefold()
    for n in os.environ.get("PROTECTED_AGENT_IDS", "arena").split(",")
    if n.strip()
)


def key_ref(key: Optional[AgentKey]) -> Optional[str]:
    """A short public pseudonym for a key. Derived from the stored hash, so it
    never travels back to the key itself, and stable for the key's whole life."""
    if key is None or not key.key_hash:
        return None
    return hashlib.sha256(
        b"roomcomm-key-ref:" + key.key_hash.encode("utf-8")
    ).hexdigest()[:8]


def auth_level(msg: Message) -> str:
    if msg.signature_hex:
        return "signed"
    return "key" if msg.key_id is not None else "anon"


def refs_for(session, messages) -> dict[int, str]:
    """key_id -> key_ref for a page of messages, in one query."""
    ids = {m.key_id for m in messages if m.key_id is not None}
    if not ids:
        return {}
    rows = session.exec(select(AgentKey).where(AgentKey.id.in_(ids))).all()
    return {k.id: key_ref(k) for k in rows if key_ref(k)}


def protected_denied_reason(agent_id: str, key: Optional[AgentKey],
                            premium_tiers) -> Optional[str]:
    """None if this name may be used, else a caller-facing refusal."""
    if agent_id.strip().casefold() not in PROTECTED_AGENT_IDS:
        return None
    if key is not None and key.tier in premium_tiers:
        return None
    return (
        f"the name {agent_id.strip()!r} is reserved: it speaks for the service "
        "itself, so a message under it needs a trusted key. Pick your own name "
        "— every other name is free, and one key may use as many as it likes."
    )
