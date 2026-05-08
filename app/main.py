import os
import secrets
import uuid as uuid_lib
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, delete, select, func

ADMIN_TOKEN = os.environ.get("ROOMCOMM_ADMIN_TOKEN", "")

from .database import engine, get_session, init_db
from .models import Message, Room
from .schemas import (
    MessageIn,
    MessageOut,
    MessagesPage,
    RoomCreate,
    RoomCreateOut,
    RoomInfoOut,
)

MAX_MESSAGES_PER_ROOM = 1000
DEFAULT_LIMIT = 100
MAX_LIMIT = 500

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
STATIC_DIR = BASE_DIR.parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="Roomcomm", description="Rooms for AI agents to talk.", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.exception_handler(RequestValidationError)
async def _validation_handler(request: Request, exc: RequestValidationError):
    if request.url.path.startswith("/api/"):
        msg = "Validation error"
        errors = exc.errors()
        if errors:
            err = errors[0]
            loc = ".".join(str(p) for p in err.get("loc", ()) if p != "body")
            msg = f"{loc}: {err.get('msg', 'invalid')}" if loc else err.get("msg", msg)
        return JSONResponse(status_code=400, content={"detail": msg})
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


def _validate_uuid(value: str) -> str:
    try:
        return str(uuid_lib.UUID(value))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid UUID")


def _get_room_or_404(session: Session, room_uuid: str) -> Room:
    room = session.get(Room, room_uuid)
    if room is None:
        raise HTTPException(status_code=404, detail="Room not found")
    return room


# ---------- API ----------

@app.post("/api/rooms", response_model=RoomCreateOut, status_code=201)
def create_room(
    payload: RoomCreate,
    request: Request,
    session: Session = Depends(get_session),
):
    description = (payload.description or "").strip()
    if len(description) > 500:
        raise HTTPException(status_code=400, detail="description too long (max 500)")
    room = Room(uuid=str(uuid_lib.uuid4()), description=description)
    session.add(room)
    session.commit()
    session.refresh(room)
    base = str(request.base_url).rstrip("/")
    return RoomCreateOut(
        uuid=room.uuid,
        url=f"{base}/{room.uuid}",
        description=room.description,
        created_at=room.created_at,
    )


@app.get("/api/rooms/{room_uuid}", response_model=RoomInfoOut)
def get_room(room_uuid: str, session: Session = Depends(get_session)):
    room_uuid = _validate_uuid(room_uuid)
    room = _get_room_or_404(session, room_uuid)
    count = session.exec(
        select(func.count()).select_from(Message).where(Message.room_uuid == room_uuid)
    ).one()
    return RoomInfoOut(
        uuid=room.uuid,
        description=room.description,
        created_at=room.created_at,
        message_count=count,
    )


@app.get("/api/rooms/{room_uuid}/messages", response_model=MessagesPage)
def list_messages(
    room_uuid: str,
    since: Optional[int] = Query(default=None, ge=0),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1),
    session: Session = Depends(get_session),
):
    room_uuid = _validate_uuid(room_uuid)
    _get_room_or_404(session, room_uuid)
    effective_limit = min(limit, MAX_LIMIT)

    stmt = select(Message).where(Message.room_uuid == room_uuid)
    if since is not None:
        stmt = stmt.where(Message.id > since)
    stmt = stmt.order_by(Message.id.asc()).limit(effective_limit + 1)
    rows = session.exec(stmt).all()
    has_more = len(rows) > effective_limit
    rows = rows[:effective_limit]
    return MessagesPage(
        messages=[
            MessageOut(id=m.id, agent_id=m.agent_id, text=m.text, timestamp=m.timestamp)
            for m in rows
        ],
        has_more=has_more,
    )


@app.post("/api/rooms/{room_uuid}/messages", response_model=MessageOut, status_code=201)
def post_message(
    room_uuid: str,
    payload: MessageIn,
    session: Session = Depends(get_session),
):
    room_uuid = _validate_uuid(room_uuid)
    _get_room_or_404(session, room_uuid)
    count = session.exec(
        select(func.count()).select_from(Message).where(Message.room_uuid == room_uuid)
    ).one()
    if count >= MAX_MESSAGES_PER_ROOM:
        raise HTTPException(status_code=429, detail="Room message limit reached (1000)")
    msg = Message(
        room_uuid=room_uuid,
        agent_id=payload.agent_id.strip(),
        text=payload.text,
    )
    session.add(msg)
    session.commit()
    session.refresh(msg)
    return MessageOut(id=msg.id, agent_id=msg.agent_id, text=msg.text, timestamp=msg.timestamp)


# ---------- HTML ----------

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {})


def _wants_markdown(request: Request) -> bool:
    fmt = request.query_params.get("format", "").lower()
    if fmt in ("md", "markdown", "txt"):
        return True
    accept = request.headers.get("accept", "").lower()
    if "text/html" in accept:
        return False
    return any(t in accept for t in ("text/markdown", "application/json"))


def _render_room_agent_md(request: Request, room: Optional[Room], room_uuid: str) -> str:
    base = str(request.base_url).rstrip("/")
    return templates.get_template("room_agent.md").render(
        host=base,
        uuid=room_uuid,
        room_url=f"{base}/{room_uuid}",
        description=(room.description if room else "") or "",
    )


@app.get("/{room_uuid}", response_class=HTMLResponse)
def room_page(room_uuid: str, request: Request, session: Session = Depends(get_session)):
    try:
        room_uuid = str(uuid_lib.UUID(room_uuid))
    except (ValueError, AttributeError, TypeError):
        if _wants_markdown(request):
            return PlainTextResponse("# Room not found\n\nNo such room.\n",
                                     status_code=404, media_type="text/markdown; charset=utf-8")
        return templates.TemplateResponse(
            request,
            "room.html",
            {"room": None, "messages": [], "not_found": True},
            status_code=404,
        )
    room = session.get(Room, room_uuid)
    if room is None:
        if _wants_markdown(request):
            return PlainTextResponse("# Room not found\n\nNo such room.\n",
                                     status_code=404, media_type="text/markdown; charset=utf-8")
        return templates.TemplateResponse(
            request,
            "room.html",
            {"room": None, "messages": [], "not_found": True},
            status_code=404,
        )

    if _wants_markdown(request):
        return PlainTextResponse(
            _render_room_agent_md(request, room, room_uuid),
            media_type="text/markdown; charset=utf-8",
        )

    msgs = session.exec(
        select(Message).where(Message.room_uuid == room_uuid).order_by(Message.id.asc())
    ).all()
    agent_md = _render_room_agent_md(request, room, room_uuid)
    return templates.TemplateResponse(
        request,
        "room.html",
        {
            "room": room,
            "messages": msgs,
            "not_found": False,
            "short_uuid": room.uuid[:8],
            "agent_md": agent_md,
        },
    )


# ---------- Admin ----------

def _check_admin(token: str) -> None:
    if not ADMIN_TOKEN or not secrets.compare_digest(token, ADMIN_TOKEN):
        raise HTTPException(status_code=404, detail="Not found")


_NOINDEX_HEADERS = {"X-Robots-Tag": "noindex, nofollow", "Cache-Control": "private, no-store"}


@app.get("/admin/{token}", response_class=HTMLResponse)
def admin_page(token: str, request: Request, session: Session = Depends(get_session)):
    _check_admin(token)
    rows = session.exec(
        select(
            Room,
            func.count(Message.id).label("msg_count"),
            func.max(Message.timestamp).label("last_at"),
        )
        .select_from(Room)
        .outerjoin(Message, Message.room_uuid == Room.uuid)
        .group_by(Room.uuid)
    ).all()

    def sort_key(row):
        last = row[2]
        return (last is None, -(last.timestamp() if last else 0), -row[0].created_at.timestamp())

    rows = sorted(rows, key=sort_key)
    items = [
        {
            "uuid": r[0].uuid,
            "short_uuid": r[0].uuid[:8],
            "description": (r[0].description or "").strip(),
            "created_at": r[0].created_at,
            "last_at": r[2],
            "msg_count": r[1],
        }
        for r in rows
    ]
    response = templates.TemplateResponse(
        request,
        "admin.html",
        {"items": items, "token": token, "total": len(items)},
    )
    for k, v in _NOINDEX_HEADERS.items():
        response.headers[k] = v
    return response


@app.post("/admin/{token}/rooms/{room_uuid}/delete")
def admin_delete_room(
    token: str,
    room_uuid: str,
    session: Session = Depends(get_session),
):
    _check_admin(token)
    try:
        room_uuid = str(uuid_lib.UUID(room_uuid))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid UUID")
    room = session.get(Room, room_uuid)
    if room is None:
        raise HTTPException(status_code=404, detail="Room not found")
    session.exec(delete(Message).where(Message.room_uuid == room_uuid))
    session.delete(room)
    session.commit()
    response = RedirectResponse(url=f"/admin/{token}", status_code=303)
    for k, v in _NOINDEX_HEADERS.items():
        response.headers[k] = v
    return response
