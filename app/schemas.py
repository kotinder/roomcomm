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


class RoomCreateOut(BaseModel):
    uuid: str
    url: str
    description: str
    created_at: datetime

    @field_serializer("created_at")
    def _ser(self, v: datetime) -> str:
        return v.strftime("%Y-%m-%dT%H:%M:%SZ")


class RoomInfoOut(BaseModel):
    uuid: str
    description: str
    created_at: datetime
    message_count: int

    @field_serializer("created_at")
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
