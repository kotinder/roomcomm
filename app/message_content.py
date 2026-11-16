"""Transport-independent inline message envelope and bounded JSON validation."""
import json
import math
from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator
from sqlmodel import Session

from .models import Message

MAX_DATA_BYTES = 16_384
CONTENT_FIELDS = ("type", "data", "schema_version", "reply_to", "thread_id")


def canonical_json(value: Any) -> str:
    """Python JSON encoding used by rc-anchor-2 (not RFC 8785/JCS)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)


def display_json(data: Optional[dict]) -> Optional[str]:
    """Lossless display text: browsers must not reserialize JSON numbers."""
    if data is None:
        return None
    return json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False)


class MessageContent(BaseModel):
    type: str = Field(default="message", min_length=1, max_length=100,
                      pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
    data: Optional[dict[str, Any]] = None
    schema_version: str = Field(default="1", min_length=1, max_length=32,
                                pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
    reply_to: Optional[int] = Field(default=None, gt=0, le=2**63 - 1)
    thread_id: Optional[str] = Field(default=None, min_length=1, max_length=128,
                                   pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")

    @model_validator(mode="after")
    def bounded_json(self):
        stack = [(self.data, 0)]
        while stack:
            value, depth = stack.pop()
            if depth > 32:
                raise ValueError("data nesting exceeds 32 levels")
            if isinstance(value, dict):
                if any(not isinstance(k, str) for k in value):
                    raise ValueError("data object keys must be strings")
                for key in value:
                    try:
                        key.encode("utf-8")
                    except UnicodeEncodeError:
                        raise ValueError("data must contain valid Unicode strings") from None
                stack.extend((v, depth + 1) for v in value.values())
            elif isinstance(value, list):
                stack.extend((v, depth + 1) for v in value)
            elif value is not None and type(value) not in (str, int, float, bool):
                raise ValueError("data must contain JSON values only")
            elif isinstance(value, str):
                try:
                    value.encode("utf-8")
                except UnicodeEncodeError:
                    raise ValueError("data must contain valid Unicode strings") from None
            elif isinstance(value, float) and not math.isfinite(value):
                raise ValueError("data numbers must be finite")
        if len(canonical_json(self.data).encode("ascii")) > MAX_DATA_BYTES:
            raise ValueError("data exceeds 16384 bytes of canonical JSON; use an attachment")
        return self


def content_fields(message) -> dict:
    return {name: getattr(message, name) for name in CONTENT_FIELDS}


def is_extended(message) -> bool:
    return content_fields(message) != {
        "type": "message", "data": None, "schema_version": "1",
        "reply_to": None, "thread_id": None,
    }


def validate_reply(session: Session, room_uuid: str, reply_to: Optional[int]) -> None:
    if reply_to is not None:
        parent = session.get(Message, reply_to)
        if parent is None or parent.room_uuid != room_uuid:
            raise ValueError("reply_to must reference an existing message in this room")
