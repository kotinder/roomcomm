"""Room-scoped Markdown file exchange — verified-keys-only, both directions.

Transport-neutral core shared by the REST handlers (main.py) and the MCP
tools (mcp_server.py), same split as quota/inbox. Content is stored on disk
content-addressed by sha256 (one blob may back rows in several rooms); DB
rows are the room-scoped references. Only UTF-8 text is accepted — this is
a channel for Markdown documents, not arbitrary binaries.
"""

import hashlib
import re
import uuid as uuid_lib
from typing import Optional

from sqlmodel import Session, func, select

from .database import FILES_DIR
from .models import AgentKey, Room, RoomFile

MAX_BYTES = 256 * 1024
MAX_FILES_PER_ROOM = 50
MAX_NAME_LEN = 100

# Whitelist sanitize — the name ends up in Content-Disposition and in agent
# prompts, so no quotes, slashes, or control chars survive.
_NAME_SAFE_RE = re.compile(r"[^\w.\- ]", re.UNICODE)


class FileError(Exception):
    """Validation/state error on the file channel, transport-neutral."""

    def __init__(self, detail: str, status: int = 400):
        super().__init__(detail)
        self.detail = detail
        self.status = status


def safe_name(raw: Optional[str]) -> str:
    """Sanitize a client-supplied filename and force the .md extension."""
    name = _NAME_SAFE_RE.sub("_", (raw or "").strip()).strip(". ")
    if not name:
        name = "file"
    if not name.lower().endswith(".md"):
        name += ".md"
    return name[-MAX_NAME_LEN:]


def validate_content(data: bytes) -> None:
    """Markdown only: non-empty UTF-8 text without NULs, within the size cap."""
    if not data:
        raise FileError("empty file")
    if len(data) > MAX_BYTES:
        raise FileError(f"file too large: {len(data)} bytes, limit {MAX_BYTES}")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise FileError("file must be UTF-8 text (Markdown) — binaries are not accepted")
    if "\x00" in text:
        raise FileError("file must not contain NUL bytes")


def store(session: Session, room: Room, key: AgentKey, agent_id: str,
          name: Optional[str], data: bytes, description: str = "") -> tuple[RoomFile, bool]:
    """Validate and persist a file into a room; (row, deduped).

    Caller has already passed the verified gate and the room's write policy.
    Dedup is per (room, sha256): re-sharing the same bytes into the same room
    returns the existing row. Commits.
    """
    validate_content(data)
    digest = hashlib.sha256(data).hexdigest()

    existing = session.exec(
        select(RoomFile).where(RoomFile.room_uuid == room.uuid,
                               RoomFile.sha256 == digest)
    ).first()
    if existing:
        return existing, True

    count = session.exec(
        select(func.count()).select_from(RoomFile).where(RoomFile.room_uuid == room.uuid)
    ).one()
    if count >= MAX_FILES_PER_ROOM:
        raise FileError(
            f"room_files_full: room file limit reached ({MAX_FILES_PER_ROOM}) — "
            "delete old files or use a new room.",
            status=429,
        )

    FILES_DIR.mkdir(parents=True, exist_ok=True)
    (FILES_DIR / f"{digest}.md").write_bytes(data)

    rf = RoomFile(
        id=str(uuid_lib.uuid4()),
        room_uuid=room.uuid,
        sha256=digest,
        name=safe_name(name),
        description=(description or "").strip()[:300],
        size_bytes=len(data),
        agent_id=agent_id.strip(),
        key_id=key.id,
    )
    session.add(rf)
    session.commit()
    session.refresh(rf)
    return rf, False


def list_room_files(session: Session, room_uuid: str) -> list[RoomFile]:
    return list(session.exec(
        select(RoomFile).where(RoomFile.room_uuid == room_uuid)
        .order_by(RoomFile.uploaded_at, RoomFile.id)
    ).all())


def get_room_file(session: Session, room_uuid: str, file_id: str) -> RoomFile:
    rf = session.get(RoomFile, file_id)
    if rf is None or rf.room_uuid != room_uuid:
        raise FileError("file not found in this room", status=404)
    return rf


def load_content(rf: RoomFile) -> bytes:
    path = FILES_DIR / f"{rf.sha256}.md"
    try:
        return path.read_bytes()
    except OSError:
        raise FileError("file content is missing from storage", status=404)


def delete(session: Session, rf: RoomFile) -> None:
    """Remove the row; drop the blob only when no other room references it.
    Commits."""
    digest = rf.sha256
    session.delete(rf)
    session.commit()
    still_referenced = session.exec(
        select(RoomFile).where(RoomFile.sha256 == digest)
    ).first()
    if still_referenced is None:
        try:
            (FILES_DIR / f"{digest}.md").unlink(missing_ok=True)
        except OSError:
            pass  # orphan blob is harmless; next identical upload rewrites it
