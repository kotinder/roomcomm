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


class Message(SQLModel, table=True):
    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_room_id", "room_uuid", "id"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    room_uuid: str = Field(foreign_key="rooms.uuid", index=True)
    agent_id: str = Field(max_length=100)
    text: str = Field(max_length=10000)
    timestamp: datetime = Field(default_factory=utcnow)
