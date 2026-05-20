from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field, field_serializer


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


class RoomCreateOut(BaseModel):
    uuid: str
    url: str
    description: str
    created_at: datetime
    is_public: bool

    @field_serializer("created_at")
    def _ser(self, v: datetime) -> str:
        return v.strftime("%Y-%m-%dT%H:%M:%SZ")


class RoomInfoOut(BaseModel):
    uuid: str
    description: str
    created_at: datetime
    message_count: int
    is_public: bool

    @field_serializer("created_at")
    def _ser(self, v: datetime) -> str:
        return v.strftime("%Y-%m-%dT%H:%M:%SZ")


class RoomListItem(BaseModel):
    uuid: str
    url: str
    description: str
    created_at: datetime
    last_activity_at: Optional[datetime]
    message_count: int

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


class MessageIn(BaseModel):
    agent_id: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=10000)


class MessageOut(BaseModel):
    id: int
    agent_id: str
    text: str
    timestamp: datetime

    @field_serializer("timestamp")
    def _ser(self, v: datetime) -> str:
        return v.strftime("%Y-%m-%dT%H:%M:%SZ")


class MessagesPage(BaseModel):
    messages: list[MessageOut]
    has_more: bool
