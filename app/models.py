from datetime import datetime, timezone
from typing import Optional
from sqlmodel import SQLModel, Field, Index


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Room(SQLModel, table=True):
    __tablename__ = "rooms"

    uuid: str = Field(primary_key=True)
    description: str = Field(default="", max_length=500)
    created_at: datetime = Field(default_factory=utcnow)
    is_public: bool = Field(default=False, index=True)
    # "standard" — claims feature available, LLM runs only on /context/refresh.
    # "premium"  — LLM extracts claims after every message (background task).
    protocol_mode: str = Field(default="standard", max_length=20)


class Message(SQLModel, table=True):
    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_room_id", "room_uuid", "id"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    room_uuid: str = Field(foreign_key="rooms.uuid", index=True)
    agent_id: str = Field(max_length=100)
    text: str = Field(max_length=10000)
    timestamp: datetime = Field(default_factory=utcnow)


class Claim(SQLModel, table=True):
    """A concrete commitment extracted from (or proposed against) a room's chat.

    Always stored in English regardless of conversation language — the LLM
    translates during extraction. Status flow: proposed → agreed (when both
    sides ack) or disputed (when arbiter finds contradiction).
    """
    __tablename__ = "claims"
    __table_args__ = (Index("ix_claims_room_status", "room_uuid", "status"),)

    id: str = Field(primary_key=True)
    room_uuid: str = Field(foreign_key="rooms.uuid", index=True)
    type: str = Field(max_length=50)  # price, quantity, delivery_date, party, payment_terms, scope, other
    value: str = Field(max_length=500)
    source_msg_id: Optional[int] = Field(default=None)
    quote: Optional[str] = Field(default=None, max_length=1000)
    proposed_by: str = Field(max_length=100)  # agent_id or "arbiter"
    status: str = Field(default="proposed", max_length=20, index=True)
    created_at: datetime = Field(default_factory=utcnow)


class ClaimAck(SQLModel, table=True):
    """Acknowledgment of a claim by a participating agent. Optional Ed25519 sig."""
    __tablename__ = "claim_acks"
    __table_args__ = (Index("ix_claim_acks_claim_agent", "claim_id", "agent_id", unique=True),)

    id: Optional[int] = Field(default=None, primary_key=True)
    claim_id: str = Field(foreign_key="claims.id", index=True)
    agent_id: str = Field(max_length=100)
    pubkey_hex: Optional[str] = Field(default=None, max_length=64)
    signature_hex: Optional[str] = Field(default=None, max_length=128)
    created_at: datetime = Field(default_factory=utcnow)


class Discrepancy(SQLModel, table=True):
    """Arbiter-flagged contradiction between a message and existing agreed context."""
    __tablename__ = "discrepancies"

    id: Optional[int] = Field(default=None, primary_key=True)
    room_uuid: str = Field(foreign_key="rooms.uuid", index=True)
    description: str = Field(max_length=1000)
    severity: str = Field(default="medium", max_length=10)  # low / medium / high
    related_msg_id: Optional[int] = Field(default=None)
    related_claim_id: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=utcnow)
    resolved: bool = Field(default=False)


class Handshake(SQLModel, table=True):
    """Final two-party signature over the agreed context snapshot."""
    __tablename__ = "handshakes"

    id: Optional[int] = Field(default=None, primary_key=True)
    room_uuid: str = Field(foreign_key="rooms.uuid", index=True)
    context_hash: str = Field(max_length=64)  # sha256 of canonical agreed snapshot
    agent_id: str = Field(max_length=100)
    pubkey_hex: Optional[str] = Field(default=None, max_length=64)
    signature_hex: Optional[str] = Field(default=None, max_length=128)
    created_at: datetime = Field(default_factory=utcnow)


class Skill(SQLModel, table=True):
    __tablename__ = "skills"

    id: str = Field(primary_key=True)
    sha256: str = Field(unique=True, index=True)
    name: str = Field(max_length=100)
    version: str = Field(max_length=50)
    description: str = Field(default="", max_length=500)
    agent_id: str = Field(max_length=100)
    author_pubkey: Optional[str] = Field(default=None, max_length=64)
    author_sig: Optional[str] = Field(default=None, max_length=128)
    size_bytes: int = Field(default=0)
    uploaded_at: datetime = Field(default_factory=utcnow)
