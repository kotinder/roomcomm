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
    # Watermark for incremental LLM processing — only messages with id >
    # last_extracted_msg_id are fed to the arbiter on the next refresh.
    last_extracted_msg_id: int = Field(default=0)


class Message(SQLModel, table=True):
    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_room_id", "room_uuid", "id"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    room_uuid: str = Field(foreign_key="rooms.uuid", index=True)
    agent_id: str = Field(max_length=100)
    text: str = Field(max_length=10000)
    timestamp: datetime = Field(default_factory=utcnow)
    # Optional Ed25519 signature by the message author.
    # If present, signed bytes = text || timestamp_iso || room_uuid || (memory_root or "")
    pubkey_hex: Optional[str] = Field(default=None, max_length=64)
    signature_hex: Optional[str] = Field(default=None, max_length=128)
    # Opaque hex string the agent uses to commit to its memory state at the
    # moment of sending. Server does not interpret it.
    memory_root: Optional[str] = Field(default=None, max_length=128)


class Claim(SQLModel, table=True):
    """A negotiation thread — one subject discussed across many messages.

    A claim is an *entity* (e.g. "Concrete delivery to site #2") with a
    current state and a ledger of revisions. The LLM arbiter matches new
    messages against existing threads via `subject_key` and either appends
    a revision to an existing thread or opens a new one.

    All subject/value text is in English regardless of the conversation
    language — the arbiter translates during extraction.
    """
    __tablename__ = "claims"
    __table_args__ = (
        Index("ix_claims_room_status", "room_uuid", "status"),
        Index("ix_claims_room_subject_key", "room_uuid", "subject_key"),
    )

    id: str = Field(primary_key=True)
    room_uuid: str = Field(foreign_key="rooms.uuid", index=True)
    subject: str = Field(max_length=200)
    subject_key: str = Field(max_length=200)  # kebab-case stable identifier for re-matching
    current_value: str = Field(max_length=500)
    # proposed | agreed | disputed | superseded | cancelled
    status: str = Field(default="proposed", max_length=20, index=True)
    # agent_id of the agent that opened the thread (first propose-revision author)
    opened_by: str = Field(max_length=100)
    last_revision_id: Optional[int] = Field(default=None)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ClaimRevision(SQLModel, table=True):
    """One entry in a claim's ledger — proposal, update, +1, contradiction, or retract.

    Each revision is part of a per-room hash chain — prev_hash references the
    row_hash of the previous revision in the same room, row_hash is the
    sha256 of (prev_hash || canonical_payload). The arbiter additionally signs
    the row's canonical payload with the platform's Ed25519 key; the signature
    is stored in arbiter_signature_hex. Together this makes the ledger
    tamper-evident even against the platform operator.
    """
    __tablename__ = "claim_revisions"
    __table_args__ = (Index("ix_revisions_claim_order", "claim_id", "id"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    claim_id: str = Field(foreign_key="claims.id", index=True)
    value: str = Field(max_length=500)
    source_msg_id: Optional[int] = Field(default=None)
    quote: Optional[str] = Field(default=None, max_length=300)
    author_agent_id: str = Field(max_length=100)
    # propose | update | confirm | contradict | retract
    kind: str = Field(max_length=20)
    # Optional Ed25519 signature by the *agent* who authored this revision
    # (when set via manual POST /revisions with signed payload).
    pubkey_hex: Optional[str] = Field(default=None, max_length=64)
    signature_hex: Optional[str] = Field(default=None, max_length=128)
    created_at: datetime = Field(default_factory=utcnow)
    # PCIS-style room-scoped hash chain + arbiter signature.
    prev_hash: Optional[str] = Field(default=None, max_length=64)
    row_hash: Optional[str] = Field(default=None, max_length=64, index=True)
    arbiter_signature_hex: Optional[str] = Field(default=None, max_length=128)


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
