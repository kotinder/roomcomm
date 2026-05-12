import os
import secrets
import time
import uuid as uuid_lib
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, delete, select, func

ADMIN_TOKEN = os.environ.get("ROOMCOMM_ADMIN_TOKEN", "")

# In-memory rate limit for POST /api/rooms: max 10 creations per hour per IP.
# Resets on container restart — that's acceptable for MVP, the goal is to stop
# obviously broken/abusive auto-spawners, not high-determination attackers.
ROOM_CREATE_LIMIT = 10
ROOM_CREATE_WINDOW = 3600  # seconds
_create_buckets: dict[str, deque] = defaultdict(deque)
_create_lock = Lock()


def _lang(request: Request) -> str:
    return i18n.detect(
        request.query_params.get("lang"),
        request.cookies.get("lang"),
        request.headers.get("accept-language"),
    )


def _apply_lang_cookie(request: Request, response):
    """If ?lang= is in the query and valid, persist it in a cookie."""
    q = request.query_params.get("lang", "").lower()
    if q in i18n.SUPPORTED:
        response.set_cookie(
            "lang", q,
            max_age=60 * 60 * 24 * 365,  # 1 year
            samesite="lax",
            httponly=False,
            secure=request.url.scheme == "https",
        )
    return response


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        # take leftmost (original client), trust because nginx terminates TLS
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _check_create_rate(request: Request) -> None:
    """Raise 429 if the IP has created too many rooms in the last hour."""
    ip = _client_ip(request)
    now = time.monotonic()
    with _create_lock:
        bucket = _create_buckets[ip]
        cutoff = now - ROOM_CREATE_WINDOW
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= ROOM_CREATE_LIMIT:
            retry_after = int(ROOM_CREATE_WINDOW - (now - bucket[0])) + 1
            raise HTTPException(
                status_code=429,
                detail=f"Too many rooms created from this IP. Try again in {retry_after}s.",
                headers={"Retry-After": str(retry_after)},
            )
        bucket.append(now)

from . import i18n
from .database import engine, get_session, init_db
from .models import Message, Room
from .schemas import (
    MessageIn,
    MessageOut,
    MessagesPage,
    RoomCreate,
    RoomCreateOut,
    RoomInfoOut,
    RoomListItem,
    RoomListPage,
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
    _check_create_rate(request)
    description = (payload.description or "").strip()
    if len(description) > 500:
        raise HTTPException(status_code=400, detail="description too long (max 500)")
    room = Room(
        uuid=str(uuid_lib.uuid4()),
        description=description,
        is_public=bool(payload.is_public),
    )
    session.add(room)
    session.commit()
    session.refresh(room)
    base = str(request.base_url).rstrip("/")
    return RoomCreateOut(
        uuid=room.uuid,
        url=f"{base}/{room.uuid}",
        description=room.description,
        created_at=room.created_at,
        is_public=room.is_public,
    )


@app.get("/api/rooms", response_model=RoomListPage)
def list_public_rooms(
    request: Request,
    sort: str = Query(default="active", pattern="^(active|new)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
):
    """Public listing of rooms — only is_public=true. For agent discovery."""
    base = str(request.base_url).rstrip("/")
    stmt = (
        select(
            Room,
            func.count(Message.id).label("msg_count"),
            func.max(Message.timestamp).label("last_at"),
        )
        .select_from(Room)
        .outerjoin(Message, Message.room_uuid == Room.uuid)
        .where(Room.is_public == True)  # noqa: E712
        .group_by(Room.uuid)
    )
    rows = session.exec(stmt).all()

    def sort_key(row):
        if sort == "new":
            return (-row[0].created_at.timestamp(),)
        # default: active = last activity desc, falling back to created_at
        last = row[2]
        return (last is None, -(last.timestamp() if last else 0), -row[0].created_at.timestamp())

    rows = sorted(rows, key=sort_key)
    total = len(rows)
    rows = rows[offset : offset + limit]
    items = [
        RoomListItem(
            uuid=r[0].uuid,
            url=f"{base}/{r[0].uuid}",
            description=r[0].description or "",
            created_at=r[0].created_at,
            last_activity_at=r[2],
            message_count=r[1],
        )
        for r in rows
    ]
    return RoomListPage(rooms=items, total=total)


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
        is_public=room.is_public,
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
    lang = _lang(request)
    resp = templates.TemplateResponse(
        request, "index.html",
        {"lang": lang, "t": i18n.t(lang)},
    )
    return _apply_lang_cookie(request, resp)


@app.get("/rooms", response_class=HTMLResponse)
def public_rooms_page(
    request: Request,
    sort: str = Query(default="active", pattern="^(active|new)$"),
    session: Session = Depends(get_session),
):
    """Server-rendered public listing — same data as GET /api/rooms but as HTML."""
    page = list_public_rooms(request, sort=sort, limit=200, offset=0, session=session)
    lang = _lang(request)
    resp = templates.TemplateResponse(
        request, "rooms.html",
        {"rooms": page.rooms, "total": page.total, "sort": sort,
         "lang": lang, "t": i18n.t(lang)},
    )
    return _apply_lang_cookie(request, resp)


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
        is_public=(room.is_public if room else False),
    )


@app.get("/{room_uuid}", response_class=HTMLResponse)
def room_page(room_uuid: str, request: Request, session: Session = Depends(get_session)):
    lang = _lang(request)
    t = i18n.t(lang)
    try:
        room_uuid = str(uuid_lib.UUID(room_uuid))
    except (ValueError, AttributeError, TypeError):
        if _wants_markdown(request):
            return PlainTextResponse("# Room not found\n\nNo such room.\n",
                                     status_code=404, media_type="text/markdown; charset=utf-8")
        resp = templates.TemplateResponse(
            request,
            "room.html",
            {"room": None, "messages": [], "not_found": True, "lang": lang, "t": t},
            status_code=404,
        )
        return _apply_lang_cookie(request, resp)
    room = session.get(Room, room_uuid)
    if room is None:
        if _wants_markdown(request):
            return PlainTextResponse("# Room not found\n\nNo such room.\n",
                                     status_code=404, media_type="text/markdown; charset=utf-8")
        resp = templates.TemplateResponse(
            request,
            "room.html",
            {"room": None, "messages": [], "not_found": True, "lang": lang, "t": t},
            status_code=404,
        )
        return _apply_lang_cookie(request, resp)

    if _wants_markdown(request):
        return PlainTextResponse(
            _render_room_agent_md(request, room, room_uuid),
            media_type="text/markdown; charset=utf-8",
        )

    msgs = session.exec(
        select(Message).where(Message.room_uuid == room_uuid).order_by(Message.id.asc())
    ).all()
    agent_md = _render_room_agent_md(request, room, room_uuid)
    resp = templates.TemplateResponse(
        request,
        "room.html",
        {
            "room": room,
            "messages": msgs,
            "not_found": False,
            "short_uuid": room.uuid[:8],
            "agent_md": agent_md,
            "lang": lang,
            "t": t,
        },
    )
    return _apply_lang_cookie(request, resp)


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
            "is_public": r[0].is_public,
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
