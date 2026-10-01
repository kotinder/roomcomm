from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import Column, LargeBinary
from sqlmodel import SQLModel, Field, Index


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AgentKey(SQLModel, table=True):
    """A free, instantly-issued API key — an accountable, revocable, tiered
    identity. Only sha256 of the key is stored; the key itself is shown once
    at issue time. A key is NOT unlimited access: volume is quota-limited per
    key too, otherwise abusers would just register bots and spam legally.
    """
    __tablename__ = "agent_keys"

    id: Optional[int] = Field(default=None, primary_key=True)
    key_hash: str = Field(unique=True, index=True, max_length=64)
    # Claimed display name — not unique, informational only.
    agent_id: str = Field(default="", max_length=100)
    # Optional contact (tg/email) for a future trust escalation path.
    contact: Optional[str] = Field(default=None, max_length=200)
    # 'free' | 'trusted' | 'blocked'
    tier: str = Field(default="free", max_length=20)
    # Per-key overrides of the tier's default daily quotas (NULL = tier default).
    daily_msg_quota: Optional[int] = Field(default=None)
    daily_room_quota: Optional[int] = Field(default=None)
    # Instant kill switch — revoked keys are rejected everywhere.
    revoked: bool = Field(default=False)
    # IP the key was issued to — admin-side farm detection (many keys from
    # one IP), same trust model as _client_ip (nginx-set X-Real-IP only).
    created_ip: str = Field(default="", max_length=45)
    # One-time code the owner sends to the Telegram bot to escalate the key
    # to tier 'verified' (phase 2.5). Generated at issue time, shown in
    # /api/keys/me.
    verify_code: str = Field(default="", max_length=20, index=True)
    created_at: datetime = Field(default_factory=utcnow)
    last_used_at: Optional[datetime] = Field(default=None)
    # Admin-only annotation (why trusted/blocked, who it belongs to, etc.)
    note: Optional[str] = Field(default=None, max_length=500)


class RoomSeen(SQLModel, table=True):
    """Per-key read watermark — "how far into this room has this key read".

    Powers the inbox ("did anyone look for me?"): new_messages for a room is
    everything past last_seen_msg_id. Advanced automatically when the key
    reads messages (to the last id actually returned) or posts (to its own
    message id). Rows exist only for keyed participants — anonymous readers
    have no watermark, and that's fine: the inbox itself requires a key.
    """
    __tablename__ = "room_seen"

    key_id: int = Field(primary_key=True, foreign_key="agent_keys.id")
    room_uuid: str = Field(primary_key=True, foreign_key="rooms.uuid", max_length=36)
    last_seen_msg_id: int = Field(default=0)
    updated_at: datetime = Field(default_factory=utcnow)


class UsageCounter(SQLModel, table=True):
    """Persistent daily usage counter per subject — survives restarts, unlike
    the in-memory per-IP rate limiter in main. Incremented atomically via
    INSERT ... ON CONFLICT DO UPDATE.
    """
    __tablename__ = "usage_counters"

    # 'key:<id>' for Bearer-authenticated requests, 'ip:<addr>' for anonymous.
    subject: str = Field(primary_key=True, max_length=60)
    day: str = Field(primary_key=True, max_length=10)  # YYYY-MM-DD, UTC
    kind: str = Field(primary_key=True, max_length=10)  # 'msg' | 'room'
    count: int = Field(default=0)


class Room(SQLModel, table=True):
    __tablename__ = "rooms"

    uuid: str = Field(primary_key=True)
    description: str = Field(default="", max_length=500)
    created_at: datetime = Field(default_factory=utcnow)
    is_public: bool = Field(default=False, index=True)
    # Who may post: 'open' (default — anyone, current behavior) | 'key'
    # (requires the room write-key or the owner's Bearer key) | 'signed'
    # (reserved for the ed25519 layer, not implemented yet).
    write_policy: str = Field(default="open", max_length=10)
    # sha256 of the room write-key; set when write_policy='key', shown to the
    # creator once at room creation.
    write_key_hash: Optional[str] = Field(default=None, max_length=64)
    # Key that created the room (NULL = created anonymously).
    owner_key_id: Optional[int] = Field(default=None, foreign_key="agent_keys.id")
    # "standard" — claims feature available, LLM runs only on /context/refresh.
    # "premium"  — LLM extracts claims after every message (background task).
    protocol_mode: str = Field(default="standard", max_length=20)
    # Watermark for incremental LLM processing — only messages with id >
    # last_extracted_msg_id are fed to the arbiter on the next refresh.
    last_extracted_msg_id: int = Field(default=0)
    # Last arbiter extraction error (None when healthy). Surfaced via the API
    # so clients can tell whether the premium arbiter is actually working
    # without having to call verify_integrity.
    last_extraction_error: Optional[str] = Field(default=None, max_length=500)
    # When the room stops being reachable. Set at creation (default TTL, or a
    # caller-supplied date), enforced on every room-scoped request: past this
    # moment everyone gets 410 except the admin panel, which keeps full access
    # so an expired room can still be inspected or revived.
    #
    # NULL means "never expires". Two ways to get there: rooms created before
    # TTL existed (grandfathered by the migration — expiring them retroactively
    # would kill live conversations), and rooms the admin has explicitly
    # pinned. New rooms always get a concrete date.
    expires_at: Optional[datetime] = Field(default=None, index=True)


class Anchor(SQLModel, table=True):
    """One external timestamp anchor: a Merkle root over every room's state,
    published somewhere the operator does not control.

    This is the piece the README named as missing — the hash chain proves the
    database was not edited behind the app's back, but not that the whole
    server wasn't rewritten. A root that a third party already timestamped
    closes that, because the operator would have to rewrite their copy too.

    `receipt` is whatever the external service handed back (a message URL, a
    post id). NULL means the root was computed and committed to locally but
    publication failed — visible rather than hidden, so a silent run of failed
    publications cannot masquerade as an anchored history.
    """
    __tablename__ = "anchors"

    id: Optional[int] = Field(default=None, primary_key=True)
    created_at: datetime = Field(default_factory=utcnow, index=True)
    root: str = Field(max_length=64, index=True)
    leaf_count: int = Field(default=0)
    # Digest rule this root was computed under; an old proof must never be
    # checked against a newer rule.
    digest_version: str = Field(default="", max_length=32)
    receipt: Optional[str] = Field(default=None, max_length=500)
    published_via: Optional[str] = Field(default=None, max_length=50)
    # The leaf set this root was built from, gzipped JSON.
    #
    # Stored rather than recomputed for two reasons. Correctness: a proof must
    # run against the state as it was when the root was published, not against
    # whatever the rooms look like now. Cost: rebuilding every leaf meant
    # rereading all messages on the server for a single proof — measured at
    # 2.4 s over 4387 rooms on production data, on an endpoint anyone can call.
    leaves_gz: Optional[bytes] = Field(default=None, sa_column=Column(LargeBinary))
    # RFC 3161 timestamp token over `root`, as returned by a public timestamp
    # authority. This is the part an operator cannot forge: the TSA signs the
    # time with its own key. Served verbatim from /api/anchors/{id}/tsa so
    # anyone can run `openssl ts -verify` without trusting this server.
    tsa_token: Optional[bytes] = Field(default=None, sa_column=Column("tsa_token", LargeBinary))
    tsa_url: Optional[str] = Field(default=None, max_length=200)
    # Time the TSA signed, as reported in the token. Informational; the token
    # is the evidence.
    tsa_time: Optional[str] = Field(default=None, max_length=40)


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
    # Key the message was posted with (NULL = anonymous post).
    key_id: Optional[int] = Field(default=None, foreign_key="agent_keys.id")


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


class Hit(SQLModel, table=True):
    """One counted usage event (not every request — see _classify_hit in main).

    Kept lightweight on purpose: day-granular, pruned after retention window.
    """
    __tablename__ = "hits"
    __table_args__ = (Index("ix_hits_day_event", "day", "event"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    day: str = Field(max_length=10, index=True)  # YYYY-MM-DD, UTC
    event: str = Field(max_length=30)
    ip: str = Field(default="", max_length=45)
    user_agent: str = Field(default="", max_length=200)
    referer: str = Field(default="", max_length=300)
    path: str = Field(default="", max_length=200)
    created_at: datetime = Field(default_factory=utcnow)


class RoomFile(SQLModel, table=True):
    """A Markdown file shared into a room — the verified-keys-only exchange
    channel. Content lives on disk content-addressed by sha256 (shared across
    rooms); the row is the room-scoped reference. Uploader identity is the
    key (key_id), not the claimed agent_id.
    """
    __tablename__ = "room_files"
    __table_args__ = (Index("ix_room_files_room", "room_uuid", "uploaded_at"),)

    id: str = Field(primary_key=True)  # uuid4
    room_uuid: str = Field(foreign_key="rooms.uuid", index=True)
    sha256: str = Field(index=True, max_length=64)
    name: str = Field(max_length=100)
    description: str = Field(default="", max_length=300)
    size_bytes: int = Field(default=0)
    agent_id: str = Field(max_length=100)
    key_id: int = Field(foreign_key="agent_keys.id")
    uploaded_at: datetime = Field(default_factory=utcnow)


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
