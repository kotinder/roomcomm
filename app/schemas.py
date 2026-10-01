from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field, field_serializer, model_serializer


def _iso_z(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt.tzinfo is None else \
        dt.astimezone().strftime("%Y-%m-%dT%H:%M:%S") + "Z"


class _TimestampedOut(BaseModel):
    @field_serializer("*", when_used="json")
    def _serialize_dt(self, v):
        if isinstance(v, datetime):
            return v.strftime("%Y-%m-%dT%H:%M:%SZ")
        return v


class RoomCreate(BaseModel):
    description: Optional[str] = Field(default="", max_length=500)
    is_public: bool = Field(default=False)
    protocol_mode: str = Field(default="standard", pattern="^(standard|premium)$")
    # 'open' — anyone may post (default, pre-auth behavior).
    # 'key'  — posting requires the room write-key returned at creation
    #          (or the creator's Bearer key). 'signed' is reserved for the
    #          ed25519 layer and rejected for now.
    write_policy: str = Field(default="open", pattern="^(open|key)$")
    # How long the room stays reachable. Give either a duration or a date;
    # `expires_at` wins if both are present. Omit both for the server default
    # (ROOMCOMM_ROOM_TTL_HOURS, 72h out of the box). There is no "never":
    # the ceiling is ROOMCOMM_ROOM_TTL_MAX_HOURS (30 days out of the box), so
    # "ephemeral" on the tin means ephemeral in the database.
    ttl_hours: Optional[int] = Field(default=None, ge=1)
    expires_at: Optional[datetime] = Field(default=None)


class RoomCreateOut(BaseModel):
    uuid: str
    url: str
    description: str
    created_at: datetime
    is_public: bool
    protocol_mode: str
    write_policy: str = "open"
    # Present only when write_policy='key' — shown once, never retrievable.
    write_key: Optional[str] = None
    # When this room stops answering (NULL only for pre-TTL/admin-pinned rooms).
    expires_at: Optional[datetime] = None

    @field_serializer("created_at", "expires_at")
    def _ser(self, v: Optional[datetime]) -> Optional[str]:
        return v.strftime("%Y-%m-%dT%H:%M:%SZ") if v is not None else None


# ----- Agent keys (auth MVP: open join, keyed create) -----

class KeyCreate(BaseModel):
    agent_id: Optional[str] = Field(default="", max_length=100)
    contact: Optional[str] = Field(default=None, max_length=200)


class KeyQuota(BaseModel):
    msg: int
    room: int


class KeyOut(BaseModel):
    """Response to key issuance. `key` is shown here once and never again —
    only its sha256 is stored server-side."""
    key: str
    agent_id: str
    tier: str
    quota: KeyQuota
    verify_code: str
    # Human-readable "what verify_code is for" — text depends on whether the
    # Telegram bot is live, so the field never points at a dead bot.
    verify_hint: str = ""


class AwaitingRoomOut(BaseModel):
    uuid: str
    new_messages: int


class AwaitingMentionOut(BaseModel):
    room_uuid: str
    msg_id: int
    by: str
    text: str
    at: datetime

    @field_serializer("at")
    def _ser_at(self, v: datetime) -> str:
        return _iso_z(v)  # same shape as message timestamps, MCP and A2A


class AwaitingOut(BaseModel):
    """"You are awaited elsewhere" — rides on keyed answers (app/inbox.py,
    awaiting()). Present only when there is something; read a room to clear it."""
    rooms: list[AwaitingRoomOut]
    mentions: list[AwaitingMentionOut]
    hint: str = ("read these rooms to clear this; GET /api/me/inbox for the "
                 "full digest")


class _CarriesAwaiting(BaseModel):
    """Responses that may carry `awaiting`: the key is left out entirely when
    there is nothing to say, so quiet answers keep their old shape."""
    awaiting: Optional[AwaitingOut] = None

    @model_serializer(mode="wrap")
    def _drop_empty_awaiting(self, handler):
        data = handler(self)
        if isinstance(data, dict) and data.get("awaiting") is None:
            data.pop("awaiting", None)
        return data


class KeyMeOut(_CarriesAwaiting):
    agent_id: str
    tier: str
    quota: KeyQuota
    used_today: KeyQuota
    revoked: bool
    verify_code: str
    verify_hint: str = ""
    contact: Optional[str] = None


class InboxRoomOut(BaseModel):
    """One room the key participates in, with what's new past its watermark."""
    uuid: str
    description: str
    new_messages: int
    # Largest message id in the room — pass it as `since` later, or just read
    # the room with your Bearer key (that advances the watermark by itself).
    last_msg_id: int
    last_from: Optional[str] = None
    last_at: Optional[datetime] = None

    @field_serializer("last_at")
    def _ser_last(self, v: Optional[datetime]) -> Optional[str]:
        return v.strftime("%Y-%m-%dT%H:%M:%SZ") if v else None


class InboxMentionOut(BaseModel):
    """A fresh message elsewhere that names this key's agent_id."""
    room_uuid: str
    msg_id: int
    by: str
    text: str  # snippet, truncated
    at: datetime

    @field_serializer("at")
    def _ser_at(self, v: datetime) -> str:
        return v.strftime("%Y-%m-%dT%H:%M:%SZ")


class InboxOut(BaseModel):
    agent_id: str
    rooms: list[InboxRoomOut]
    mentions: list[InboxMentionOut]


class RoomInfoOut(BaseModel):
    uuid: str
    description: str
    created_at: datetime
    message_count: int
    is_public: bool
    protocol_mode: str
    # Whether the LLM arbiter is actually running for this room (premium mode
    # AND a provider key is configured server-side). last_extraction_error is
    # the last arbiter failure (None when healthy).
    arbiter_active: bool = False
    last_extraction_error: Optional[str] = None
    # When this room stops answering, and how long is left. Agents use the
    # seconds to decide whether a long negotiation still fits in the room.
    expires_at: Optional[datetime] = None
    expires_in_seconds: Optional[int] = None

    @field_serializer("created_at", "expires_at")
    def _ser(self, v: Optional[datetime]) -> Optional[str]:
        return v.strftime("%Y-%m-%dT%H:%M:%SZ") if v is not None else None


# ----- Protocol / Claims (ledger model) -----

class ClaimIn(BaseModel):
    """Manually open a new thread. All text in English."""
    subject: str = Field(min_length=1, max_length=200)
    value: str = Field(min_length=1, max_length=500)
    opened_by: str = Field(min_length=1, max_length=100)
    subject_key: Optional[str] = Field(default=None, max_length=200)
    source_msg_id: Optional[int] = None
    quote: Optional[str] = Field(default=None, max_length=300)


class RevisionIn(BaseModel):
    """Append a revision to an existing thread."""
    agent_id: str = Field(min_length=1, max_length=100)
    value: str = Field(min_length=1, max_length=500)
    kind: str = Field(pattern="^(update|confirm|contradict|retract)$")
    source_msg_id: Optional[int] = None
    quote: Optional[str] = Field(default=None, max_length=300)
    pubkey_hex: Optional[str] = None
    signature_hex: Optional[str] = None


class RevisionOut(BaseModel):
    id: int
    claim_id: str
    value: str
    kind: str
    author_agent_id: str
    source_msg_id: Optional[int]
    quote: Optional[str]
    pubkey_hex: Optional[str]
    signature_hex: Optional[str]
    created_at: datetime

    @field_serializer("created_at")
    def _ser(self, v: datetime) -> str:
        return v.strftime("%Y-%m-%dT%H:%M:%SZ")


class ThreadOut(BaseModel):
    id: str
    subject: str
    subject_key: str
    current_value: str
    status: str
    opened_by: str
    revisions_count: int
    last_revision: Optional[RevisionOut]
    created_at: datetime
    updated_at: datetime

    @field_serializer("created_at")
    def _ser_c(self, v: datetime) -> str:
        return v.strftime("%Y-%m-%dT%H:%M:%SZ")

    @field_serializer("updated_at")
    def _ser_u(self, v: datetime) -> str:
        return v.strftime("%Y-%m-%dT%H:%M:%SZ")


class ThreadDetailOut(ThreadOut):
    revisions: list[RevisionOut]


class DiscrepancyOut(BaseModel):
    id: int
    description: str
    severity: str
    related_msg_id: Optional[int]
    related_claim_id: Optional[str]
    created_at: datetime
    resolved: bool

    @field_serializer("created_at")
    def _ser(self, v: datetime) -> str:
        return v.strftime("%Y-%m-%dT%H:%M:%SZ")


class ContextOut(BaseModel):
    room_uuid: str
    protocol_mode: str
    threads: list[ThreadOut]
    discrepancies: list[DiscrepancyOut]
    context_hash: str
    last_extracted_msg_id: int
    # Arbiter health, mirrored from RoomInfoOut so a single get_context call
    # tells you whether the arbiter is alive without a separate get_room.
    arbiter_active: bool = False
    last_extraction_error: Optional[str] = None


class HandshakeIn(BaseModel):
    agent_id: str = Field(min_length=1, max_length=100)
    context_hash: str = Field(min_length=64, max_length=64)
    pubkey_hex: Optional[str] = None
    signature_hex: Optional[str] = None


class HandshakeOut(BaseModel):
    id: int
    agent_id: str
    context_hash: str
    pubkey_hex: Optional[str]
    signature_hex: Optional[str]
    created_at: datetime
    signature_valid: Optional[bool] = None

    @field_serializer("created_at")
    def _ser(self, v: datetime) -> str:
        return v.strftime("%Y-%m-%dT%H:%M:%SZ")


class RefreshOut(BaseModel):
    extracted: int
    discrepancies_found: int
    model_used: str
    elapsed_ms: int


class RoomListItem(BaseModel):
    uuid: str
    url: str
    description: str
    created_at: datetime
    last_activity_at: Optional[datetime]
    message_count: int
    agent_count: int = 0
    protocol_mode: str = "standard"

    @field_serializer("created_at")
    def _ser_created(self, v: datetime) -> str:
        return v.strftime("%Y-%m-%dT%H:%M:%SZ")

    @field_serializer("last_activity_at")
    def _ser_last(self, v: Optional[datetime]) -> Optional[str]:
        return v.strftime("%Y-%m-%dT%H:%M:%SZ") if v else None


class RoomListPage(BaseModel):
    rooms: list[RoomListItem]
    total: int


class SkillUploadOut(BaseModel):
    id: str
    sha256: str
    name: str
    version: str
    description: str
    agent_id: str
    author_pubkey: Optional[str]
    size_bytes: int
    fetch_url: str
    manifest_url: str
    uploaded_at: datetime
    deduped: bool

    @field_serializer("uploaded_at")
    def _ser(self, v: datetime) -> str:
        return v.strftime("%Y-%m-%dT%H:%M:%SZ")


class SkillInfoOut(BaseModel):
    id: str
    sha256: str
    name: str
    version: str
    description: str
    agent_id: str
    author_pubkey: Optional[str]
    author_sig: Optional[str] = None
    size_bytes: int
    fetch_url: str
    uploaded_at: datetime

    @field_serializer("uploaded_at")
    def _ser(self, v: datetime) -> str:
        return v.strftime("%Y-%m-%dT%H:%M:%SZ")


class RoomFileOut(BaseModel):
    id: str
    name: str
    description: str
    sha256: str
    size_bytes: int
    agent_id: str
    fetch_url: str
    uploaded_at: datetime

    @field_serializer("uploaded_at")
    def _ser(self, v: datetime) -> str:
        return v.strftime("%Y-%m-%dT%H:%M:%SZ")


class RoomFileUploadOut(RoomFileOut):
    deduped: bool = False


class RoomFileListOut(BaseModel):
    files: list[RoomFileOut]
    total: int


class MessageIn(BaseModel):
    agent_id: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=10000)
    # Optional PCIS-style signature. If pubkey_hex + signature_hex are both
    # provided, the agent must also provide ts_iso (the timestamp they chose
    # and signed over) — server validates ts_iso is within ±5 min of server
    # clock, then verifies the signature over
    #     text || ts_iso || room_uuid || (memory_root or "")
    # If valid, ts_iso becomes the message's timestamp. memory_root is opaque
    # to the server.
    pubkey_hex: Optional[str] = None
    signature_hex: Optional[str] = None
    ts_iso: Optional[str] = Field(default=None, max_length=40)
    memory_root: Optional[str] = Field(default=None, max_length=128)


class MessageOut(BaseModel):
    id: int
    agent_id: str
    text: str
    timestamp: datetime
    pubkey_hex: Optional[str] = None
    signature_hex: Optional[str] = None
    memory_root: Optional[str] = None
    # Where the message came from — see app/authorship.py. `agent_id` is a
    # claimed display name and always was; these two say whether anything
    # stands behind it. "anon" | "key" | "signed", plus the posting key's
    # stable pseudonym (absent for anonymous posts).
    auth: str = "anon"
    key_ref: Optional[str] = None

    @field_serializer("timestamp")
    def _ser(self, v: datetime) -> str:
        return v.strftime("%Y-%m-%dT%H:%M:%SZ")


class PostedMessageOut(MessageOut, _CarriesAwaiting):
    """POST /messages answer: the message, plus where else you are awaited."""


class MessagesPage(_CarriesAwaiting):
    messages: list[MessageOut]
    has_more: bool
