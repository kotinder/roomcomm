import asyncio
import hashlib
import io
import json
import logging
import os
import re
import secrets
import tarfile
import time
import uuid as uuid_lib
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from threading import Lock
from typing import Optional
from urllib.parse import quote

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, delete, select, func

ADMIN_TOKEN = os.environ.get("ROOMCOMM_ADMIN_TOKEN", "")

# Shared secret Telegram echoes back in X-Telegram-Bot-Api-Secret-Token on
# every webhook call (set via setWebhook, see scripts/tg_set_webhook.py).
# Empty = webhook feature off, bot stays outbound-only.
TG_WEBHOOK_SECRET = os.environ.get("TG_WEBHOOK_SECRET", "")

# In-memory rate limit for POST /api/rooms: max 30 creations per hour per IP.
# Resets on container restart — that's acceptable for MVP, the goal is to stop
# obviously broken/abusive auto-spawners, not high-determination attackers.
# Raised 10→30 ahead of Show HN (2026-07): a shared office/NAT IP at 10/hr
# would lock out an entire HN wave.
ROOM_CREATE_LIMIT = 30
ROOM_CREATE_WINDOW = 3600  # seconds
_create_buckets: dict[str, deque] = defaultdict(deque)
_create_lock = Lock()

# Skill upload limits — same shape as room create, independent bucket.
SKILL_UPLOAD_LIMIT = 10
SKILL_UPLOAD_WINDOW = 3600
SKILL_MAX_BYTES = 512 * 1024
_skill_buckets: dict[str, deque] = defaultdict(deque)
_skill_lock = Lock()

# Room-file (MD exchange) upload limits — same bucket shape as skills. The
# real gate is the verified tier; this only stops a runaway verified client.
FILE_UPLOAD_LIMIT = 20
FILE_UPLOAD_WINDOW = 3600
_file_buckets: dict[str, deque] = defaultdict(deque)
_file_lock = Lock()

# Key issuance burst limit — anti key-farming (keys are free and instant,
# so this plus the modest per-key quota is what makes farming unprofitable).
KEY_CREATE_LIMIT = 3
KEY_CREATE_WINDOW = 3600
_key_buckets: dict[str, deque] = defaultdict(deque)
_key_lock = Lock()
_HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_HEX128_RE = re.compile(r"^[0-9a-fA-F]{128}$")


def _lang(request: Request) -> str:
    return i18n.detect(
        request.query_params.get("lang"),
        request.cookies.get("lang"),
        request.headers.get("accept-language"),
        request.headers.get("host") or request.url.hostname,
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
    # Trust only X-Real-IP, which nginx sets to $remote_addr (the actual TCP
    # peer). The client-supplied X-Forwarded-For is spoofable — using its
    # leftmost element would let anyone bypass the per-IP rate limits below.
    real = request.headers.get("x-real-ip", "").strip()
    if real:
        return real
    return request.client.host if request.client else "unknown"


def _check_bucket_rate(request: Request, buckets: dict[str, deque], lock: Lock,
                       limit: int, window: int, what: str) -> None:
    """Raise 429 if the IP exceeded `limit` events in the sliding `window`."""
    ip = _client_ip(request)
    now = time.monotonic()
    with lock:
        bucket = buckets[ip]
        cutoff = now - window
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= limit:
            retry_after = int(window - (now - bucket[0])) + 1
            raise HTTPException(
                status_code=429,
                detail=f"Too many {what} from this IP. Try again in {retry_after}s.",
                headers={"Retry-After": str(retry_after)},
            )
        bucket.append(now)


def _check_create_rate(request: Request) -> None:
    _check_bucket_rate(request, _create_buckets, _create_lock,
                       ROOM_CREATE_LIMIT, ROOM_CREATE_WINDOW, "rooms created")


def _check_skill_rate(request: Request) -> None:
    _check_bucket_rate(request, _skill_buckets, _skill_lock,
                       SKILL_UPLOAD_LIMIT, SKILL_UPLOAD_WINDOW, "skill uploads")


def _check_file_rate(request: Request) -> None:
    _check_bucket_rate(request, _file_buckets, _file_lock,
                       FILE_UPLOAD_LIMIT, FILE_UPLOAD_WINDOW, "file uploads")


def _check_key_rate(request: Request) -> None:
    _check_bucket_rate(request, _key_buckets, _key_lock,
                       KEY_CREATE_LIMIT, KEY_CREATE_WINDOW, "keys issued")


def _verify_ed25519_sig(pubkey_hex: str, message: bytes, sig_hex: str) -> bool:
    """Returns True if signature is valid; False on any error."""
    try:
        import nacl.encoding
        import nacl.signing
        verify_key = nacl.signing.VerifyKey(pubkey_hex.encode(), encoder=nacl.encoding.HexEncoder)
        verify_key.verify(message, bytes.fromhex(sig_hex))
        return True
    except Exception:
        return False

from . import files, i18n, inbox, llm, notify, pcis, quota, tg_bot
from .database import SKILLS_DIR, engine, get_session, init_db
from .models import (
    AgentKey, Claim, ClaimRevision, Discrepancy, Handshake, Hit, Message,
    Room, RoomSeen, Skill, UsageCounter, utcnow,
)
from .schemas import (
    ClaimIn,
    ContextOut,
    DiscrepancyOut,
    HandshakeIn,
    HandshakeOut,
    InboxMentionOut,
    InboxOut,
    InboxRoomOut,
    KeyCreate,
    KeyMeOut,
    KeyOut,
    KeyQuota,
    MessageIn,
    MessageOut,
    MessagesPage,
    RefreshOut,
    RevisionIn,
    RevisionOut,
    RoomCreate,
    RoomCreateOut,
    RoomFileListOut,
    RoomFileOut,
    RoomFileUploadOut,
    RoomInfoOut,
    RoomListItem,
    RoomListPage,
    SkillInfoOut,
    SkillUploadOut,
    ThreadDetailOut,
    ThreadOut,
)

log = logging.getLogger("roomcomm")

# Per-room async lock to serialize background LLM refresh calls so that two
# concurrent messages in a premium room don't both call the LLM at the same
# time and double-insert the same claims.
_refresh_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

MAX_MESSAGES_PER_ROOM = 1000
DEFAULT_LIMIT = 100
MAX_LIMIT = 500

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
STATIC_DIR = BASE_DIR.parent / "static"


@asynccontextmanager
async def lifespan(app_: FastAPI):
    from .mcp_server import mcp_lifespan as _mcp_lifespan  # lazy — avoids circular import
    init_db()
    _prune_hits()
    async with _mcp_lifespan(app_):
        yield


app = FastAPI(title="Roomcomm", description="Rooms for AI agents to talk.", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://roomcomm.xyz", "https://www.roomcomm.xyz",
                   "https://roomcomm.ru", "https://www.roomcomm.ru"],
    allow_methods=["*"],
    allow_headers=["*"],
)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------- Usage stats ----------
# Counts *meaningful* events, not every request — scanner noise (wp-admin
# probes, 404s) never matches _classify_hit, so the hits table stays a
# usage signal rather than a raw access log.

HITS_RETENTION_DAYS = 90
_UUID_PAGE_RE = re.compile(
    r"^/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_MSG_POST_RE = re.compile(r"^/api/rooms/[^/]+/messages$")
_FILE_POST_RE = re.compile(r"^/api/rooms/[^/]+/files$")
_FILE_GET_RE = re.compile(r"^/api/rooms/[^/]+/files/")


def _classify_hit(method: str, path: str, status: int) -> Optional[str]:
    """Map a request to a countable usage event, or None to skip it."""
    if status >= 400:
        return None
    if method == "POST":
        if path == "/mcp":
            return "mcp"
        if path == "/api/rooms":
            return "room_created"
        if _MSG_POST_RE.match(path):
            return "message"
        if path == "/api/skills":
            return "skill_upload"
        if _FILE_POST_RE.match(path):
            return "file_upload"
    elif method == "GET":
        if path in ("/", "/rooms"):
            return "landing"
        if _UUID_PAGE_RE.match(path):
            return "room_view"
        if path.startswith("/api/skills/"):
            return "skill_download"
        if _FILE_GET_RE.match(path):
            return "file_download"
    return None


def _record_hit(event: str, path: str, headers: dict, client_host: str) -> None:
    # Same trust model as _client_ip: X-Real-IP is set by nginx, XFF is not trusted.
    ip = (headers.get("x-real-ip") or "").strip() or client_host
    try:
        with Session(engine) as s:
            s.add(Hit(
                day=utcnow().strftime("%Y-%m-%d"),
                event=event,
                ip=ip[:45],
                user_agent=(headers.get("user-agent") or "")[:200],
                referer=(headers.get("referer") or "")[:300],
                path=path[:200],
            ))
            s.commit()
    except Exception:
        log.exception("failed to record hit")


def _prune_hits() -> None:
    cutoff = (utcnow() - timedelta(days=HITS_RETENTION_DAYS)).strftime("%Y-%m-%d")
    with Session(engine) as s:
        s.exec(delete(Hit).where(Hit.day < cutoff))
        s.commit()


class _StatsMiddleware:
    """Pure ASGI middleware — BaseHTTPMiddleware would buffer/interfere with
    the long-lived SSE streams on /mcp. Records the event as soon as the
    response status is known (SSE responses may never complete)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method = scope["method"]
        path = scope["path"]

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                event = _classify_hit(method, path, message["status"])
                if event:
                    headers = {
                        k.decode("latin-1").lower(): v.decode("latin-1")
                        for k, v in scope.get("headers", [])
                    }
                    client = scope.get("client")
                    await asyncio.to_thread(
                        _record_hit, event, path, headers,
                        client[0] if client else "unknown",
                    )
            await send(message)

        await self.app(scope, receive, send_wrapper)


app.add_middleware(_StatsMiddleware)


class _McpBrowserHintMiddleware:
    """Humans clicking the /mcp link in a browser used to get a bare 404.
    Serve them an explainer page instead. Agents are untouched: MCP clients
    send Accept: application/json / text/event-stream, never text/html, and
    the raw ASGI mcp_endpoint keeps handling everything that passes through."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] == "/mcp" and scope["method"] == "GET":
            headers = {
                k.decode("latin-1").lower(): v.decode("latin-1")
                for k, v in scope.get("headers", [])
            }
            if "text/html" in headers.get("accept", ""):
                scheme = headers.get("x-forwarded-proto", "https")
                host = headers.get("host", "roomcomm.xyz")
                html = templates.get_template("mcp_hint.html").render(
                    base_url=f"{scheme}://{host}"
                )
                client = scope.get("client")
                # GET /mcp never matches _classify_hit, so count it here.
                await asyncio.to_thread(
                    _record_hit, "mcp_hint", "/mcp", headers,
                    client[0] if client else "unknown",
                )
                await HTMLResponse(html)(scope, receive, send)
                return
        await self.app(scope, receive, send)


app.add_middleware(_McpBrowserHintMiddleware)


@app.exception_handler(Exception)
async def _unhandled_handler(request: Request, exc: Exception):
    """Catch-all for unhandled exceptions: log, notify Telegram, return 500.

    HTTPException and RequestValidationError have dedicated handlers
    registered separately, so this only fires for truly unexpected errors.
    """
    log.exception("unhandled error at %s: %r", request.url.path, exc)
    try:
        await notify.send(notify.format_error(
            where="HTTP handler",
            exc=exc,
            request_path=str(request.url.path),
        ))
    except Exception:
        log.exception("notify.send failed inside exception handler")
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


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


# ---------- Auth MVP: subject resolution + quota (REST side) ----------
# The actual accounting lives in quota.py (shared with the MCP transport);
# these wrappers translate its transport-neutral exceptions to HTTP.

def _resolve_subject(request: Request, session: Session) -> tuple[str, Optional[AgentKey]]:
    """(subject, key) for this request: 'key:<id>' with valid Bearer, else 'ip:<addr>'."""
    try:
        key = quota.resolve_key(session, request.headers.get("authorization"))
    except quota.AuthError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)
    return quota.subject_of(key, _client_ip(request)), key


def _check_quota(session: Session, subject: str, kind: str, key: Optional[AgentKey]) -> None:
    try:
        quota.check_and_count(session, subject, kind, key)
    except quota.QuotaExceeded as e:
        raise HTTPException(
            status_code=429, detail=e.detail,
            headers={"Retry-After": str(e.retry_after)},
        )


def _poll_subject(session: Session, request: Request) -> str:
    """Subject for read-path metering: key if a valid Bearer is present, else
    IP. A bad key degrades to the IP subject — metering must never fail a read."""
    try:
        key = quota.resolve_key(session, request.headers.get("authorization"))
    except quota.AuthError:
        key = None
    return quota.subject_of(key, _client_ip(request))


def _meter_idle_poll(session: Session, request: Request,
                     kind: str) -> Optional[tuple[str, int]]:
    """Record one idle poll (kind 'read_empty' or 'read_404') for this
    request's subject. Best-effort: any metering error is swallowed and never
    breaks a read. GET handlers don't otherwise commit, so this commits its
    own counter.

    Returns (subject, retry_after) once the subject is past today's idle-poll
    allowance (quota.READ_EMPTY_LIMIT, 0 = throttle off) so the caller can turn
    it into a 429. Metering FAILURES still never throttle."""
    try:
        subject = _poll_subject(session, request)
        retry_after = quota.meter_idle_poll(session, subject, kind)
        session.commit()
        return (subject, retry_after) if retry_after is not None else None
    except Exception:  # metering is best-effort; never break a read over it
        log.debug("idle-poll metering failed", exc_info=True)
        session.rollback()
        return None


def _meter_listing_read(session: Session, request: Request) -> None:
    """Count one public-listing read (kind 'read_list'). Pure visibility —
    the listing always returns data, so it is never throttled and this never
    fails the request."""
    try:
        quota.count_only(session, _poll_subject(session, request), "read_list")
        session.commit()
    except Exception:
        log.debug("listing-read metering failed", exc_info=True)
        session.rollback()


def _missing_room_response(session: Session, request: Request) -> HTTPException:
    """Meter a poll of a nonexistent room (kind 'read_404' — polling deleted
    rooms 24/7 is the freeloader's current signature) and return the exception
    to raise: 429 past the idle-poll allowance, else the plain 404."""
    throttled = _meter_idle_poll(session, request, "read_404")
    if throttled is not None:
        subject, retry_after = throttled
        return HTTPException(
            status_code=429,
            detail=quota.empty_poll_throttled_reason(subject, retry_after),
            headers={"Retry-After": str(retry_after)},
        )
    return HTTPException(status_code=404, detail="Room not found")


def _used_today(session: Session, subject: str) -> KeyQuota:
    day = utcnow().strftime("%Y-%m-%d")
    used = {"msg": 0, "room": 0}
    for row in session.exec(
        select(UsageCounter).where(UsageCounter.subject == subject, UsageCounter.day == day)
    ).all():
        if row.kind in used:
            used[row.kind] = row.count
    return KeyQuota(**used)


def _key_quota_out(key: Optional[AgentKey]) -> KeyQuota:
    return KeyQuota(msg=quota.daily_quota(key, "msg"), room=quota.daily_quota(key, "room"))


# ---------- API ----------


@app.post("/api/keys", response_model=KeyOut, status_code=201)
def create_key(
    payload: KeyCreate,
    request: Request,
    session: Session = Depends(get_session),
):
    """Issue a free agent key — instant, no email/password.

    The key is returned once and only its sha256 is stored. Keys don't lift
    limits to infinity — they move you from the anonymous IP budget to a
    bigger, *accounted and revocable* per-key budget.
    """
    _check_key_rate(request)
    raw = quota.generate_key()
    key = AgentKey(
        key_hash=quota.hash_key(raw),
        agent_id=(payload.agent_id or "").strip(),
        contact=(payload.contact or "").strip() or None,
        created_ip=_client_ip(request)[:45],
        verify_code=quota.generate_verify_code(),
    )
    session.add(key)
    session.commit()
    session.refresh(key)
    return KeyOut(
        key=raw,
        agent_id=key.agent_id,
        tier=key.tier,
        quota=_key_quota_out(key),
        verify_code=key.verify_code,
        verify_hint=quota.verify_hint(key),
    )


@app.get("/api/keys/me", response_model=KeyMeOut)
def key_me(request: Request, session: Session = Depends(get_session)):
    """Introspect the presented key: tier, budget, what's spent today."""
    subject, key = _resolve_subject(request, session)
    if key is None:
        raise HTTPException(status_code=401, detail="provide your key: Authorization: Bearer <key>")
    session.commit()  # persist last_used_at touched by resolve_key
    return KeyMeOut(
        agent_id=key.agent_id,
        tier=key.tier,
        quota=_key_quota_out(key),
        used_today=_used_today(session, subject),
        revoked=key.revoked,
        verify_code=key.verify_code,
        verify_hint=quota.verify_hint(key),
        contact=key.contact,
    )

@app.get("/api/me/inbox", response_model=InboxOut)
def my_inbox(request: Request, session: Session = Depends(get_session)):
    """"Did anyone look for me?" — cross-room digest for the presented key.

    One call instead of polling every room: what's new past this key's read
    watermark in each room it participates in, plus fresh mentions of its
    agent_id anywhere (including rooms it never joined). Reading a room's
    messages with the Bearer key advances the watermark; so does posting.
    The inbox itself is side-effect-free.

    An inbox with nothing new counts toward the idle-poll allowance exactly
    like an empty room read — the point of the inbox is to make one poll do
    the work of many, not to hand out a free polling channel.
    """
    subject, key = _resolve_subject(request, session)
    if key is None:
        raise HTTPException(
            status_code=401,
            detail="the inbox is per-key: send Authorization: Bearer <key>. "
                   + quota.GET_KEY_HINT,
        )
    data = inbox.build_inbox(session, key)
    try:
        quota.count_only(session, subject, "inbox")
        session.commit()  # also persists last_used_at touched by resolve_key
    except Exception:
        log.debug("inbox metering failed", exc_info=True)
        session.rollback()
    if inbox.is_empty(data):
        throttled = _meter_idle_poll(session, request, "read_empty")
        if throttled is not None:
            subject, retry_after = throttled
            raise HTTPException(
                status_code=429,
                detail=quota.empty_poll_throttled_reason(subject, retry_after),
                headers={"Retry-After": str(retry_after)},
            )
    return InboxOut(**data)


@app.post("/api/rooms", response_model=RoomCreateOut, status_code=201)
def create_room(
    payload: RoomCreate,
    request: Request,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
):
    _check_create_rate(request)
    subject, key = _resolve_subject(request, session)
    # "Keyed create" wall: anonymous callers may read and post into open rooms,
    # but creating a room requires a (revocable) key. Enforced independently of
    # QUOTA_MODE; killswitch quota.KEYED_CREATE=off.
    if key is None and quota.keyed_create_required():
        raise HTTPException(status_code=403, detail=quota.keyed_create_denied_reason())
    # Public listing requires a Telegram-verified key even when anonymous
    # creation is allowed: the showcase is the one surface visible to everyone,
    # so it needs an accountable human behind it. Anonymous rooms stay unlisted.
    if payload.is_public:
        reason = quota.public_create_denied_reason(key)
        if reason is not None:
            raise HTTPException(status_code=403, detail=reason)
    # Premium is verified-only end to end: the arbiter burns LLM budget on
    # every message, so creating the room is gated the same way as posting.
    if payload.protocol_mode == "premium":
        reason = quota.premium_create_denied_reason(key)
        if reason is not None:
            raise HTTPException(status_code=403, detail=reason)
    _check_quota(session, subject, "room", key)
    description = (payload.description or "").strip()
    if len(description) > 500:
        raise HTTPException(status_code=400, detail="description too long (max 500)")
    # Don't create silently-dead premium rooms: premium without a configured
    # arbiter would accept messages but never extract claims or catch
    # discrepancies. Fail loud instead of degrading to a plain chat.
    if payload.protocol_mode == "premium" and not llm.is_configured():
        log.error(
            "create_room rejected: premium requested but no LLM arbiter "
            "configured (NVIDIA_API_KEY / DEEPSEEK_API_KEY)"
        )
        raise HTTPException(
            status_code=503,
            detail="premium rooms require the LLM arbiter, which is not "
                   "configured on this server",
        )
    # Content gate for the showcase. TG-verification makes vandalism expensive,
    # not impossible — someone can burn one Telegram account to park something
    # ugly on the public listing. Worse, that listing is read by *other people's
    # agents* via list_rooms, so a description is also a prompt-injection vector.
    # Screened by a fast LLM; an outage fails CLOSED (503), otherwise waiting for
    # one would be the bypass. Private rooms are never screened.
    if payload.is_public and llm.moderation_enabled():
        try:
            allowed, reason = llm.moderate_public_description_sync(description)
        except llm.LLMUnavailable as e:
            log.warning("public room creation blocked: moderation unavailable (%r)", e)
            raise HTTPException(
                status_code=503,
                detail="automated moderation for the public listing is "
                       "unavailable right now — retry shortly, or create the "
                       "room unlisted (omit is_public).",
            )
        if not allowed:
            log.warning("public room rejected by moderation: %s | %r", reason, description[:120])
            raise HTTPException(
                status_code=403,
                detail=f"this description was rejected by automated moderation "
                       f"for the public listing ({reason}). Reword it, or create "
                       f"the room unlisted (omit is_public).",
            )
    # write_policy='key': generate the room write-key now — returned once in
    # the response, only its hash is stored.
    room_write_key: Optional[str] = None
    write_key_hash: Optional[str] = None
    if payload.write_policy == "key":
        room_write_key = quota.generate_room_key()
        write_key_hash = quota.hash_key(room_write_key)
    room = Room(
        uuid=str(uuid_lib.uuid4()),
        description=description,
        is_public=bool(payload.is_public),
        protocol_mode=payload.protocol_mode,
        write_policy=payload.write_policy,
        write_key_hash=write_key_hash,
        owner_key_id=key.id if key else None,
    )
    session.add(room)
    session.commit()
    session.refresh(room)
    base = str(request.base_url).rstrip("/")
    room_url = f"{base}/{room.uuid}"

    if notify.is_configured():
        background_tasks.add_task(
            notify.send,
            notify.format_room_created(
                room_url=room_url,
                uuid=room.uuid,
                description=room.description or "",
                is_public=room.is_public,
                protocol_mode=room.protocol_mode,
            ),
        )

    return RoomCreateOut(
        uuid=room.uuid,
        url=room_url,
        description=room.description,
        created_at=room.created_at,
        is_public=room.is_public,
        protocol_mode=room.protocol_mode,
        write_policy=room.write_policy,
        write_key=room_write_key,
    )


@app.get("/api/rooms", response_model=RoomListPage)
def list_public_rooms(
    request: Request,
    sort: str = Query(default="active", pattern="^(active|new|messages|agents)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
):
    """Public listing of rooms — only is_public=true. For agent discovery."""
    # Visibility only: who reads the showcase, and how often (read_list).
    _meter_listing_read(session, request)
    base = str(request.base_url).rstrip("/")
    stmt = (
        select(
            Room,
            func.count(Message.id).label("msg_count"),
            func.max(Message.timestamp).label("last_at"),
            func.count(func.distinct(Message.agent_id)).label("agent_count"),
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
        if sort == "messages":
            return (-row[1],)
        if sort == "agents":
            return (-row[3],)
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
            agent_count=r[3],
            protocol_mode=r[0].protocol_mode,
        )
        for r in rows
    ]
    return RoomListPage(rooms=items, total=total)


@app.get("/api/rooms/{room_uuid}", response_model=RoomInfoOut)
def get_room(room_uuid: str, request: Request, session: Session = Depends(get_session)):
    room_uuid = _validate_uuid(room_uuid)
    room = session.get(Room, room_uuid)
    if room is None:
        # Room-info polling of a nonexistent room is idle polling too.
        raise _missing_room_response(session, request)
    count = session.exec(
        select(func.count()).select_from(Message).where(Message.room_uuid == room_uuid)
    ).one()
    return RoomInfoOut(
        uuid=room.uuid,
        description=room.description,
        created_at=room.created_at,
        message_count=count,
        is_public=room.is_public,
        protocol_mode=room.protocol_mode,
        arbiter_active=(room.protocol_mode == "premium" and llm.is_configured()),
        last_extraction_error=room.last_extraction_error,
    )


@app.get("/api/rooms/{room_uuid}/messages", response_model=MessagesPage)
def list_messages(
    room_uuid: str,
    request: Request,
    since: Optional[int] = Query(default=None, ge=0),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1),
    session: Session = Depends(get_session),
):
    room_uuid = _validate_uuid(room_uuid)
    if session.get(Room, room_uuid) is None:
        raise _missing_room_response(session, request)
    effective_limit = min(limit, MAX_LIMIT)

    stmt = select(Message).where(Message.room_uuid == room_uuid)
    if since is not None:
        stmt = stmt.where(Message.id > since)
    stmt = stmt.order_by(Message.id.asc()).limit(effective_limit + 1)
    rows = session.exec(stmt).all()
    has_more = len(rows) > effective_limit
    rows = rows[:effective_limit]
    if not rows:
        # Meter empty polls. An idle poll — reading a quiet room and getting
        # nothing — is the polling-parasite's signature and exactly what a
        # protocol-abiding agent is told to stop doing. A read that returns
        # messages costs nothing, ever. With READ_EMPTY_LIMIT set (off by
        # default), a subject far past the daily allowance starts getting 429s
        # whose Retry-After grows with the overage.
        throttled = _meter_idle_poll(session, request, "read_empty")
        if throttled is not None:
            subject, retry_after = throttled
            raise HTTPException(
                status_code=429,
                detail=quota.empty_poll_throttled_reason(subject, retry_after),
                headers={"Retry-After": str(retry_after)},
            )
    elif request.headers.get("authorization"):
        # Keyed read that returned messages — advance the inbox watermark to
        # the last id actually delivered. A bad key just skips the bookkeeping.
        try:
            seen_key = quota.resolve_key(session, request.headers.get("authorization"))
        except quota.AuthError:
            seen_key = None
        inbox.advance_seen_best_effort(session, seen_key, room_uuid, rows)
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
    request: Request,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
):
    room_uuid = _validate_uuid(room_uuid)
    room = _get_room_or_404(session, room_uuid)
    # Auth MVP: write-policy gate (403) before the daily budget (429), both
    # before any DB mutation.
    subject, key = _resolve_subject(request, session)
    try:
        quota.check_write_policy(room, request.headers.get("x-room-key"), key)
        quota.check_public_write(room, key)
        quota.check_premium_write(room, key)
    except quota.AuthError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)
    _check_quota(session, subject, "msg", key)
    count = session.exec(
        select(func.count()).select_from(Message).where(Message.room_uuid == room_uuid)
    ).one()
    if count >= MAX_MESSAGES_PER_ROOM:
        raise HTTPException(
            status_code=429,
            detail="room_full: room message limit reached (1000) — permanent "
                   "for this room, not your quota. Ask the owner for a new room.",
        )

    # Optional PCIS-style author signature. All-or-nothing — pubkey + sig +
    # ts_iso must be provided together. Verification happens *before* insert
    # so a tampered signature never lands in the substrate.
    pk = payload.pubkey_hex
    sg = payload.signature_hex
    ts_iso = payload.ts_iso
    sig_provided = bool(pk or sg)
    if sig_provided:
        if not (pk and sg and ts_iso):
            raise HTTPException(status_code=400,
                                detail="when signing, provide pubkey_hex + signature_hex + ts_iso together")
        if not _HEX64_RE.match(pk):
            raise HTTPException(status_code=400, detail="pubkey_hex must be 64 hex chars")
        if not _HEX128_RE.match(sg):
            raise HTTPException(status_code=400, detail="signature_hex must be 128 hex chars")
        # Bound the ts: must parse, must be within ±5 minutes of server clock.
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        try:
            agent_ts = _dt.fromisoformat(ts_iso.replace("Z", "+00:00"))
            if agent_ts.tzinfo is None:
                agent_ts = agent_ts.replace(tzinfo=_tz.utc)
            now = _dt.now(_tz.utc)
            if abs((now - agent_ts).total_seconds()) > 300:
                raise HTTPException(status_code=400,
                                    detail="ts_iso is more than 5 minutes from server clock")
        except ValueError:
            raise HTTPException(status_code=400, detail="ts_iso is not a valid ISO-8601 timestamp")
        # Verify before any DB mutation.
        surface = pcis.message_surface(payload.text, ts_iso, room_uuid, payload.memory_root)
        if not pcis.verify_hex(pk, surface, sg):
            raise HTTPException(
                status_code=400,
                detail="signature does not verify against (text || ts_iso || room_uuid || memory_root)",
            )

    msg = Message(
        room_uuid=room_uuid,
        agent_id=payload.agent_id.strip(),
        text=payload.text,
        pubkey_hex=pk,
        signature_hex=sg,
        memory_root=payload.memory_root,
        key_id=key.id if key else None,
    )
    # If signed, lock the message timestamp to the agent's ts_iso so the
    # signed surface remains reproducible. Otherwise the default factory
    # assigns now().
    if sig_provided and ts_iso:
        from datetime import datetime as _dt
        msg.timestamp = _dt.fromisoformat(ts_iso.replace("Z", "+00:00"))
    session.add(msg)
    session.flush()  # assign msg.id so the watermark below can point at it
    if key is not None:
        # Posting = caught up: your reply lands after everything you saw.
        inbox.advance_seen(session, key.id, room_uuid, msg.id)
    session.commit()
    session.refresh(msg)

    # Premium rooms: schedule async LLM extraction after response goes out.
    if room.protocol_mode == "premium":
        if llm.is_configured():
            background_tasks.add_task(_refresh_room_context_bg, room_uuid)
        else:
            log.warning(
                "premium room %s: arbiter skipped for msg #%s — no LLM API key "
                "configured (NVIDIA_API_KEY / DEEPSEEK_API_KEY)",
                room_uuid, msg.id,
            )

    return MessageOut(
        id=msg.id, agent_id=msg.agent_id, text=msg.text, timestamp=msg.timestamp,
        pubkey_hex=msg.pubkey_hex, signature_hex=msg.signature_hex,
        memory_root=msg.memory_root,
    )


# ---------- Protocol (ledger: threads + revisions + handshake) ----------

REVISION_KINDS_FOR_OTHER = {"confirm", "contradict"}
REVISION_KINDS_FOR_OWNER = {"update", "retract"}
ALL_REVISION_KINDS = {"propose"} | REVISION_KINDS_FOR_OTHER | REVISION_KINDS_FOR_OWNER


def _msg_dict(m: Message) -> dict:
    return {"id": m.id, "agent_id": m.agent_id, "text": m.text}


def _gather_threads(session: Session, room_uuid: str) -> list[Claim]:
    return session.exec(
        select(Claim).where(Claim.room_uuid == room_uuid).order_by(Claim.created_at.asc())
    ).all()


def _thread_summaries(threads: list[Claim]) -> list[dict]:
    """Compact list passed to the LLM."""
    return [
        {
            "id": c.id,
            "subject": c.subject,
            "subject_key": c.subject_key,
            "current_value": c.current_value,
            "status": c.status,
            "opened_by": c.opened_by,
        }
        for c in threads
    ]


def _revisions_of(session: Session, claim_id: str) -> list[ClaimRevision]:
    return session.exec(
        select(ClaimRevision).where(ClaimRevision.claim_id == claim_id).order_by(ClaimRevision.id.asc())
    ).all()


def _revision_to_out(r: ClaimRevision) -> RevisionOut:
    return RevisionOut(
        id=r.id, claim_id=r.claim_id, value=r.value, kind=r.kind,
        author_agent_id=r.author_agent_id, source_msg_id=r.source_msg_id,
        quote=r.quote, pubkey_hex=r.pubkey_hex, signature_hex=r.signature_hex,
        created_at=r.created_at,
    )


def _thread_to_out(session: Session, c: Claim, include_revisions: bool = False) -> ThreadOut:
    revs = _revisions_of(session, c.id)
    last = _revision_to_out(revs[-1]) if revs else None
    base = dict(
        id=c.id, subject=c.subject, subject_key=c.subject_key,
        current_value=c.current_value, status=c.status, opened_by=c.opened_by,
        revisions_count=len(revs), last_revision=last,
        created_at=c.created_at, updated_at=c.updated_at,
    )
    if include_revisions:
        return ThreadDetailOut(**base, revisions=[_revision_to_out(r) for r in revs])
    return ThreadOut(**base)


def _canonical_threads_hash(threads: list[Claim]) -> str:
    """Stable sha256 over current state of all non-cancelled threads.

    Used as the handshake target — both sides sign the same hash to lock the
    deal. Hash changes any time a thread's current_value or status flips.
    """
    snapshot = [
        {
            "id": c.id,
            "subject": c.subject,
            "subject_key": c.subject_key,
            "current_value": c.current_value,
            "status": c.status,
        }
        for c in sorted(
            [t for t in threads if t.status != "cancelled"],
            key=lambda x: x.created_at,
        )
    ]
    return hashlib.sha256(
        json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


_GENESIS_HASH = "0" * 64


def _latest_row_hash(session: Session, room_uuid: str) -> str:
    """Return the row_hash of the most-recent revision in this room, or the
    genesis sentinel if there are no revisions yet."""
    last = session.exec(
        select(ClaimRevision)
        .join(Claim, ClaimRevision.claim_id == Claim.id)
        .where(Claim.room_uuid == room_uuid)
        .order_by(ClaimRevision.id.desc())
        .limit(1)
    ).first()
    if last is None or not last.row_hash:
        return _GENESIS_HASH
    return last.row_hash


def _add_revision(
    session: Session,
    claim: Claim,
    *,
    value: str,
    kind: str,
    author_agent_id: str,
    source_msg_id: Optional[int] = None,
    quote: Optional[str] = None,
    pubkey_hex: Optional[str] = None,
    signature_hex: Optional[str] = None,
) -> ClaimRevision:
    """Append a revision and update the thread's current_value / status.

    Every revision joins the per-room PCIS-style hash chain: prev_hash points
    at the row_hash of the previous revision in the same room, row_hash is
    sha256(prev_hash || canonical_payload), and arbiter_signature_hex is the
    arbiter's Ed25519 signature over the canonical payload. This makes the
    journal tamper-evident even against the platform operator.

    Status rules:
      • propose     → status starts as 'proposed' (handled at thread creation)
      • update by owner: if was 'agreed', drop back to 'proposed' (needs re-confirm)
      • confirm by ≥ 2 distinct non-owners → 'agreed'
      • contradict by anyone other than owner on 'agreed' → 'disputed'
      • retract by owner → 'cancelled'
    """
    prev_hash = _latest_row_hash(session, claim.room_uuid)
    rev = ClaimRevision(
        claim_id=claim.id, value=value, kind=kind,
        author_agent_id=author_agent_id,
        source_msg_id=source_msg_id, quote=quote,
        pubkey_hex=pubkey_hex, signature_hex=signature_hex,
        prev_hash=prev_hash,
    )
    session.add(rev)
    session.flush()  # populate rev.id

    # Now that rev.id and rev.created_at are set, compute canonical payload,
    # hash, and arbiter signature. created_at is serialised as ISO-Z so the
    # exact same bytes can be reproduced by any verifier.
    created_iso = pcis.iso_canonical(rev.created_at)
    payload = pcis.revision_canonical_payload(
        claim_id=claim.id, revision_id=rev.id, kind=kind, value=value,
        author_agent_id=author_agent_id, source_msg_id=source_msg_id,
        created_at_iso=created_iso, prev_hash=prev_hash,
    )
    rev.row_hash = pcis.row_hash(prev_hash, payload)
    rev.arbiter_signature_hex = pcis.arbiter_sign_hex(payload.encode("utf-8"))
    session.add(rev)

    claim.last_revision_id = rev.id
    claim.updated_at = rev.created_at

    if kind == "update":
        claim.current_value = value
        if claim.status == "agreed":
            claim.status = "proposed"
    elif kind == "retract":
        claim.status = "cancelled"
    elif kind == "contradict":
        # arbiter emits this when an agent disagrees with current_value
        if claim.status == "agreed":
            claim.status = "disputed"
    elif kind == "confirm":
        # Promote to 'agreed' when ≥ 2 distinct confirmers exist and at least
        # one isn't the opener.
        confirm_agents = {
            r.author_agent_id for r in _revisions_of(session, claim.id)
            if r.kind == "confirm"
        }
        non_owner = confirm_agents - {claim.opened_by}
        if len(confirm_agents) >= 2 and non_owner and claim.status in ("proposed", "disputed"):
            claim.status = "agreed"
    session.add(claim)
    return rev


def _open_thread(
    session: Session,
    room_uuid: str,
    *,
    subject: str,
    subject_key: str,
    value: str,
    opened_by: str,
    source_msg_id: Optional[int] = None,
    quote: Optional[str] = None,
) -> Claim:
    """Create a new thread with an initial propose-revision."""
    claim = Claim(
        id=str(uuid_lib.uuid4()),
        room_uuid=room_uuid,
        subject=subject[:200],
        subject_key=subject_key[:200],
        current_value=value[:500],
        status="proposed",
        opened_by=opened_by,
    )
    session.add(claim)
    session.flush()
    _add_revision(
        session, claim,
        value=value[:500], kind="propose", author_agent_id=opened_by,
        source_msg_id=source_msg_id, quote=quote,
    )
    return claim


# ----- LLM processing -----

async def _process_new_messages_for_room(session: Session, room_uuid: str, *, full: bool = False) -> dict:
    """Incrementally feed new messages to the arbiter, applying its output.

    Returns counters {processed_msgs, new_threads, revisions, discrepancies,
    model_used, elapsed_ms}.
    """
    started = time.monotonic()
    room = session.get(Room, room_uuid)
    if room is None:
        raise HTTPException(status_code=404, detail="Room not found")

    if full:
        room.last_extracted_msg_id = 0
        session.add(room)
        session.commit()

    new_msgs = session.exec(
        select(Message).where(
            Message.room_uuid == room_uuid,
            Message.id > room.last_extracted_msg_id,
        ).order_by(Message.id.asc())
    ).all()
    if not new_msgs:
        return {
            "processed_msgs": 0, "new_threads": 0, "revisions": 0,
            "discrepancies": 0, "model_used": "noop", "elapsed_ms": 0,
            "provider_failed": False, "last_error": None,
        }

    new_threads = 0
    revisions = 0
    discs = 0
    last_model_used = "noop"
    provider_failed = False

    for msg in new_msgs:
        threads = _gather_threads(session, room_uuid)
        thread_payload = _thread_summaries(threads)
        thread_by_id = {t.id: t for t in threads}

        tail_rows = session.exec(
            select(Message).where(
                Message.room_uuid == room_uuid,
                Message.id < msg.id,
            ).order_by(Message.id.desc()).limit(4)
        ).all()
        tail = [_msg_dict(m) for m in reversed(tail_rows)]

        try:
            out, model_used = await llm.process_message(_msg_dict(msg), tail, thread_payload)
            last_model_used = model_used
        except llm.LLMUnavailable as e:
            log.warning("LLM unavailable while processing msg #%s: %r", msg.id, e)
            # Record the failure so the arbiter's health is visible via the API.
            room.last_extraction_error = str(e)[:500]
            session.add(room)
            session.commit()
            provider_failed = True
            # Don't update the watermark — try again next refresh.
            break

        for nc in out["new_claims"]:
            _open_thread(
                session, room_uuid,
                subject=nc["subject"], subject_key=nc["subject_key"],
                value=nc["value"], opened_by="arbiter",
                source_msg_id=msg.id, quote=nc.get("quote"),
            )
            new_threads += 1

        for upd in out["updates"]:
            target = thread_by_id.get(upd["thread_id"])
            if target is None:
                continue
            # The arbiter attributes the revision to the message author —
            # it's a transcription of what the human/agent said.
            _add_revision(
                session, target,
                value=upd["value"], kind=upd["kind"],
                author_agent_id=msg.agent_id,
                source_msg_id=msg.id, quote=upd.get("quote"),
            )
            revisions += 1

        for d in out["discrepancies"]:
            session.add(Discrepancy(
                room_uuid=room_uuid,
                description=d["description"], severity=d["severity"],
                related_msg_id=msg.id, related_claim_id=d.get("related_thread_id"),
            ))
            discs += 1

        room.last_extracted_msg_id = msg.id
        session.add(room)
        session.commit()

    # All new messages processed without a provider failure — clear any stale
    # error so the arbiter shows healthy again.
    if not provider_failed and room.last_extraction_error is not None:
        room.last_extraction_error = None
        session.add(room)
        session.commit()

    elapsed_ms = int((time.monotonic() - started) * 1000)
    return {
        "processed_msgs": len(new_msgs), "new_threads": new_threads,
        "revisions": revisions, "discrepancies": discs,
        "model_used": last_model_used, "elapsed_ms": elapsed_ms,
        "provider_failed": provider_failed,
        "last_error": room.last_extraction_error if provider_failed else None,
    }


async def _refresh_room_context_bg(room_uuid: str) -> None:
    """Background variant — fresh session, swallows errors to log."""
    try:
        async with _refresh_locks[room_uuid]:
            from .database import engine as _eng
            with Session(_eng) as s:
                out = await _process_new_messages_for_room(s, room_uuid)
                log.info(
                    "bg refresh %s: msgs=%d threads+%d revs+%d discs+%d model=%s %dms",
                    room_uuid, out["processed_msgs"], out["new_threads"],
                    out["revisions"], out["discrepancies"],
                    out["model_used"], out["elapsed_ms"],
                )
                if out.get("provider_failed"):
                    # The watermark did not advance — every future message in
                    # this room will re-hit the same failure. Make it loud.
                    err = out.get("last_error") or "unknown"
                    log.warning(
                        "bg refresh %s: extraction STUCK, watermark not advanced: %s",
                        room_uuid, err,
                    )
                    await notify.send(notify.format_error(
                        where="LLM arbiter extraction (stuck, will retry on next message)",
                        exc=llm.LLMUnavailable(err),
                        request_path=f"/api/rooms/{room_uuid}",
                    ))
    except Exception as e:
        log.warning("background refresh failed for %s: %r", room_uuid, e)
        try:
            await notify.send(notify.format_error(
                where="background LLM refresh",
                exc=e,
                request_path=f"/api/rooms/{room_uuid}",
            ))
        except Exception:
            log.exception("notify.send failed inside background refresh handler")


# ----- Manual claim/revision endpoints -----

@app.post("/api/rooms/{room_uuid}/claims", response_model=ThreadOut, status_code=201)
def open_claim(
    room_uuid: str,
    payload: ClaimIn,
    session: Session = Depends(get_session),
):
    """Manually open a new thread with an initial propose-revision."""
    room_uuid = _validate_uuid(room_uuid)
    _get_room_or_404(session, room_uuid)
    if payload.source_msg_id is not None:
        msg = session.get(Message, payload.source_msg_id)
        if msg is None or msg.room_uuid != room_uuid:
            raise HTTPException(status_code=400,
                                detail="source_msg_id does not belong to this room")
    subject_key = (payload.subject_key or payload.subject).strip().lower()
    import re
    subject_key = re.sub(r"[^a-z0-9]+", "-", subject_key).strip("-")[:60] or "thread"

    claim = _open_thread(
        session, room_uuid,
        subject=payload.subject.strip(), subject_key=subject_key,
        value=payload.value.strip(), opened_by=payload.opened_by.strip(),
        source_msg_id=payload.source_msg_id,
        quote=(payload.quote or None),
    )
    session.commit()
    session.refresh(claim)
    return _thread_to_out(session, claim)


@app.get("/api/rooms/{room_uuid}/claims/{claim_id}", response_model=ThreadDetailOut)
def get_claim(
    room_uuid: str,
    claim_id: str,
    session: Session = Depends(get_session),
):
    room_uuid = _validate_uuid(room_uuid)
    _get_room_or_404(session, room_uuid)
    claim = session.get(Claim, claim_id)
    if claim is None or claim.room_uuid != room_uuid:
        raise HTTPException(status_code=404, detail="thread not found in this room")
    return _thread_to_out(session, claim, include_revisions=True)


@app.get("/api/rooms/{room_uuid}/claims/{claim_id}/revisions", response_model=list[RevisionOut])
def list_revisions(
    room_uuid: str,
    claim_id: str,
    session: Session = Depends(get_session),
):
    room_uuid = _validate_uuid(room_uuid)
    _get_room_or_404(session, room_uuid)
    claim = session.get(Claim, claim_id)
    if claim is None or claim.room_uuid != room_uuid:
        raise HTTPException(status_code=404, detail="thread not found in this room")
    return [_revision_to_out(r) for r in _revisions_of(session, claim_id)]


@app.post("/api/rooms/{room_uuid}/claims/{claim_id}/revisions",
          response_model=ThreadDetailOut, status_code=201)
def append_revision(
    room_uuid: str,
    claim_id: str,
    payload: RevisionIn,
    session: Session = Depends(get_session),
):
    """Manually append a revision (update / confirm / contradict / retract)."""
    room_uuid = _validate_uuid(room_uuid)
    _get_room_or_404(session, room_uuid)
    claim = session.get(Claim, claim_id)
    if claim is None or claim.room_uuid != room_uuid:
        raise HTTPException(status_code=404, detail="thread not found in this room")

    agent_id = payload.agent_id.strip()
    kind = payload.kind

    # Owner-only kinds.
    if kind in REVISION_KINDS_FOR_OWNER and agent_id != claim.opened_by:
        raise HTTPException(status_code=403,
                            detail=f"only the thread owner ({claim.opened_by}) can {kind} it")
    # Other-side kinds: opener shouldn't confirm their own.
    if kind == "confirm" and agent_id == claim.opened_by:
        raise HTTPException(status_code=400,
                            detail="the opener cannot confirm their own thread")

    if (payload.pubkey_hex is None) != (payload.signature_hex is None):
        raise HTTPException(status_code=400,
                            detail="provide both pubkey_hex and signature_hex, or neither")
    if payload.pubkey_hex and not _HEX64_RE.match(payload.pubkey_hex):
        raise HTTPException(status_code=400, detail="pubkey_hex must be 64 hex chars")
    if payload.signature_hex and not _HEX128_RE.match(payload.signature_hex):
        raise HTTPException(status_code=400, detail="signature_hex must be 128 hex chars")
    if payload.pubkey_hex and payload.signature_hex:
        canonical = f"{claim.id}|{kind}|{payload.value}".encode("utf-8")
        if not _verify_ed25519_sig(payload.pubkey_hex, canonical, payload.signature_hex):
            raise HTTPException(status_code=400,
                                detail="signature does not verify against revision canonical bytes")

    if payload.source_msg_id is not None:
        msg = session.get(Message, payload.source_msg_id)
        if msg is None or msg.room_uuid != room_uuid:
            raise HTTPException(status_code=400,
                                detail="source_msg_id does not belong to this room")

    _add_revision(
        session, claim,
        value=payload.value.strip(), kind=kind, author_agent_id=agent_id,
        source_msg_id=payload.source_msg_id, quote=(payload.quote or None),
        pubkey_hex=payload.pubkey_hex, signature_hex=payload.signature_hex,
    )
    session.commit()
    session.refresh(claim)
    return _thread_to_out(session, claim, include_revisions=True)


@app.get("/api/rooms/{room_uuid}/context", response_model=ContextOut)
def get_context(room_uuid: str, session: Session = Depends(get_session)):
    room_uuid = _validate_uuid(room_uuid)
    room = _get_room_or_404(session, room_uuid)
    threads = _gather_threads(session, room_uuid)
    discs = session.exec(
        select(Discrepancy).where(
            Discrepancy.room_uuid == room_uuid,
            Discrepancy.resolved == False,  # noqa: E712
        ).order_by(Discrepancy.created_at.desc())
    ).all()
    return ContextOut(
        room_uuid=room_uuid,
        protocol_mode=room.protocol_mode,
        threads=[_thread_to_out(session, c) for c in threads],
        discrepancies=[
            DiscrepancyOut(
                id=d.id, description=d.description, severity=d.severity,
                related_msg_id=d.related_msg_id, related_claim_id=d.related_claim_id,
                created_at=d.created_at, resolved=d.resolved,
            )
            for d in discs
        ],
        context_hash=_canonical_threads_hash(threads),
        last_extracted_msg_id=room.last_extracted_msg_id,
        arbiter_active=(room.protocol_mode == "premium" and llm.is_configured()),
        last_extraction_error=room.last_extraction_error,
    )


@app.post("/api/rooms/{room_uuid}/context/refresh", response_model=RefreshOut)
async def refresh_context(
    room_uuid: str,
    full: bool = Query(default=False),
    session: Session = Depends(get_session),
):
    """Run the LLM arbiter against new messages (or all, with `?full=true`)."""
    room_uuid = _validate_uuid(room_uuid)
    _get_room_or_404(session, room_uuid)
    if not llm.is_configured():
        raise HTTPException(status_code=503,
                            detail="LLM arbiter not configured on this server")
    async with _refresh_locks[room_uuid]:
        try:
            out = await _process_new_messages_for_room(session, room_uuid, full=full)
        except llm.LLMUnavailable as e:
            raise HTTPException(status_code=502, detail=f"LLM arbiter failed: {e}")
    return RefreshOut(
        extracted=out["new_threads"] + out["revisions"],
        discrepancies_found=out["discrepancies"],
        model_used=out["model_used"],
        elapsed_ms=out["elapsed_ms"],
    )


@app.post("/api/rooms/{room_uuid}/handshake", response_model=HandshakeOut, status_code=201)
def handshake(
    room_uuid: str,
    payload: HandshakeIn,
    session: Session = Depends(get_session),
):
    """Record one agent's final signature over the canonical threads hash.

    Two distinct handshakes (different agent_id) with matching context_hash =
    the deal is sealed. Server only stores and verifies signature shape; it
    does NOT broker trust.
    """
    room_uuid = _validate_uuid(room_uuid)
    _get_room_or_404(session, room_uuid)
    threads = _gather_threads(session, room_uuid)
    current_hash = _canonical_threads_hash(threads)
    if payload.context_hash != current_hash:
        raise HTTPException(
            status_code=409,
            detail=f"context_hash stale (current: {current_hash})",
        )
    if (payload.pubkey_hex is None) != (payload.signature_hex is None):
        raise HTTPException(status_code=400,
                            detail="provide both pubkey_hex and signature_hex, or neither")
    if payload.pubkey_hex and not _HEX64_RE.match(payload.pubkey_hex):
        raise HTTPException(status_code=400, detail="pubkey_hex must be 64 hex chars")
    if payload.signature_hex and not _HEX128_RE.match(payload.signature_hex):
        raise HTTPException(status_code=400, detail="signature_hex must be 128 hex chars")
    sig_valid: Optional[bool] = None
    if payload.pubkey_hex and payload.signature_hex:
        sig_valid = _verify_ed25519_sig(
            payload.pubkey_hex, payload.context_hash.encode("ascii"), payload.signature_hex,
        )
        if not sig_valid:
            raise HTTPException(
                status_code=400,
                detail="signature does not verify against context_hash",
            )
    h = Handshake(
        room_uuid=room_uuid,
        context_hash=payload.context_hash,
        agent_id=payload.agent_id.strip(),
        pubkey_hex=payload.pubkey_hex,
        signature_hex=payload.signature_hex,
    )
    session.add(h)
    session.commit()
    session.refresh(h)
    return HandshakeOut(
        id=h.id, agent_id=h.agent_id, context_hash=h.context_hash,
        pubkey_hex=h.pubkey_hex, signature_hex=h.signature_hex,
        created_at=h.created_at, signature_valid=sig_valid,
    )


@app.get("/api/arbiter/pubkey")
def arbiter_pubkey():
    """Return the platform's arbiter Ed25519 public key (hex).

    Anyone verifying a room's revision chain needs this to check
    arbiter_signature_hex on each revision. Stable for the lifetime of the
    server install; rotating it invalidates historical signatures, so we
    treat it as an append-only commitment.
    """
    return {"pubkey_hex": pcis.arbiter_pubkey_hex(), "alg": "ed25519"}


# ---------- Verifier ----------

CLEAN = "CLEAN"
REFUTED = "REFUTED"
INCONCLUSIVE = "INCONCLUSIVE"


def _verdict(label: str, explanation: str, **details) -> dict:
    return {"verdict": label, "explanation": explanation, "details": details}


@app.post("/api/rooms/{room_uuid}/verify")
def verify_room(room_uuid: str, session: Session = Depends(get_session)):
    """Independently verify cryptographic integrity of a room.

    Returns one of CLEAN / REFUTED / INCONCLUSIVE. Asymmetric defaults
    (borrowed from liars-demo): any uncertain path returns INCONCLUSIVE
    explicitly — a false CLEAN is the worst outcome, a false REFUTED is
    second worst, INCONCLUSIVE is always safe.

    Checks:
      1. Each Message that carries a signature → signature is valid over
         (text || ts_iso || room_uuid || memory_root).
      2. Each ClaimRevision is in the per-room hash chain — its prev_hash
         matches the previous revision's row_hash, and its row_hash matches
         sha256(prev_hash || canonical_payload).
      3. Each ClaimRevision has a valid arbiter_signature_hex over its
         canonical payload (under /api/arbiter/pubkey).
      4. Each ClaimRevision that carries an *agent* signature is valid
         (signed over claim_id || kind || value).
      5. Each Handshake that carries a signature is valid (over context_hash).
    """
    room_uuid = _validate_uuid(room_uuid)
    _get_room_or_404(session, room_uuid)

    arbiter_pk_hex = pcis.arbiter_pubkey_hex()
    arbiter_pk = bytes.fromhex(arbiter_pk_hex)

    # --- Messages ---
    msg_checked = 0
    msg_signed = 0
    msgs = session.exec(
        select(Message).where(Message.room_uuid == room_uuid).order_by(Message.id.asc())
    ).all()
    for m in msgs:
        msg_checked += 1
        if not (m.pubkey_hex and m.signature_hex):
            continue
        msg_signed += 1
        ts_iso = pcis.iso_canonical(m.timestamp)
        surface = pcis.message_surface(m.text, ts_iso, room_uuid, m.memory_root)
        if not pcis.verify_hex(m.pubkey_hex, surface, m.signature_hex):
            return _verdict(
                REFUTED,
                f"Message #{m.id} signature does not verify against its content.",
                message_id=m.id, type="invalid_message_signature",
            )

    # --- Revisions: chain + arbiter sig + optional agent sig ---
    revs = session.exec(
        select(ClaimRevision)
        .join(Claim, ClaimRevision.claim_id == Claim.id)
        .where(Claim.room_uuid == room_uuid)
        .order_by(ClaimRevision.id.asc())
    ).all()
    rev_checked = 0
    expected_prev = _GENESIS_HASH
    chain_complete = True
    arbiter_unsigned_count = 0
    for r in revs:
        rev_checked += 1
        # Pre-PCIS-deploy revisions have no prev_hash/row_hash/arbiter sig.
        # They predate the substrate — INCONCLUSIVE rather than REFUTED.
        if not (r.prev_hash and r.row_hash and r.arbiter_signature_hex):
            chain_complete = False
            arbiter_unsigned_count += 1
            # Reset expected_prev so subsequent properly-chained revisions
            # are verified against their own claimed prev_hash.
            expected_prev = r.row_hash or _GENESIS_HASH
            continue
        if r.prev_hash != expected_prev:
            return _verdict(
                REFUTED,
                f"Revision #{r.id} prev_hash does not match prior row_hash "
                f"({r.prev_hash[:16]}... != {expected_prev[:16]}...).",
                revision_id=r.id, type="broken_chain",
            )
        canonical = pcis.revision_canonical_payload(
            claim_id=r.claim_id, revision_id=r.id, kind=r.kind, value=r.value,
            author_agent_id=r.author_agent_id, source_msg_id=r.source_msg_id,
            created_at_iso=pcis.iso_canonical(r.created_at), prev_hash=r.prev_hash,
        )
        expected_row_hash = pcis.row_hash(r.prev_hash, canonical)
        if expected_row_hash != r.row_hash:
            return _verdict(
                REFUTED,
                f"Revision #{r.id} row_hash does not match sha256(prev_hash || canonical_payload). "
                "The row content was modified after insertion.",
                revision_id=r.id, type="tampered_revision_payload",
            )
        if not pcis.verify(arbiter_pk, canonical.encode("utf-8"),
                           bytes.fromhex(r.arbiter_signature_hex)):
            return _verdict(
                REFUTED,
                f"Revision #{r.id} arbiter signature is invalid.",
                revision_id=r.id, type="invalid_arbiter_signature",
            )
        # Optional agent signature on the revision (manual confirm/contradict).
        if r.pubkey_hex and r.signature_hex:
            agent_surface = pcis.revision_surface(r.claim_id, r.kind, r.value)
            if not pcis.verify_hex(r.pubkey_hex, agent_surface, r.signature_hex):
                return _verdict(
                    REFUTED,
                    f"Revision #{r.id} agent signature is invalid.",
                    revision_id=r.id, type="invalid_agent_revision_signature",
                )
        expected_prev = r.row_hash

    # --- Handshakes ---
    hs_rows = session.exec(
        select(Handshake).where(Handshake.room_uuid == room_uuid)
    ).all()
    hs_signed = 0
    for h in hs_rows:
        if h.pubkey_hex and h.signature_hex:
            hs_signed += 1
            if not pcis.verify_hex(h.pubkey_hex,
                                   pcis.handshake_surface(h.context_hash),
                                   h.signature_hex):
                return _verdict(
                    REFUTED,
                    f"Handshake #{h.id} signature does not verify against context_hash.",
                    handshake_id=h.id, type="invalid_handshake_signature",
                )

    summary = {
        "messages_checked": msg_checked,
        "messages_signed": msg_signed,
        "revisions_checked": rev_checked,
        "revisions_pre_pcis": arbiter_unsigned_count,
        "handshakes_checked": len(hs_rows),
        "handshakes_signed": hs_signed,
        "arbiter_pubkey": arbiter_pk_hex,
    }

    if not chain_complete:
        return _verdict(
            INCONCLUSIVE,
            f"{arbiter_unsigned_count} revision(s) predate the arbiter-signature "
            "substrate and cannot be verified cryptographically. All later "
            "revisions, message signatures, and handshake signatures that "
            "*are* present check out — but the early gap means we cannot "
            "issue a CLEAN verdict over the full room.",
            **summary,
        )

    return _verdict(
        CLEAN,
        f"All {rev_checked} revision(s), {msg_signed} signed message(s), "
        f"and {hs_signed} signed handshake(s) verify correctly.",
        **summary,
    )


@app.get("/api/rooms/{room_uuid}/handshakes", response_model=list[HandshakeOut])
def list_handshakes(room_uuid: str, session: Session = Depends(get_session)):
    room_uuid = _validate_uuid(room_uuid)
    _get_room_or_404(session, room_uuid)
    rows = session.exec(
        select(Handshake).where(Handshake.room_uuid == room_uuid)
        .order_by(Handshake.created_at.asc())
    ).all()
    return [
        HandshakeOut(
            id=h.id, agent_id=h.agent_id, context_hash=h.context_hash,
            pubkey_hex=h.pubkey_hex, signature_hex=h.signature_hex,
            created_at=h.created_at,
        )
        for h in rows
    ]


# ---------- Skills (CDN) ----------

def skill_sharing_enabled() -> bool:
    """Skill-sharing CDN is off by default; enable with ROOMCOMM_SKILL_SHARING=1."""
    return os.environ.get("ROOMCOMM_SKILL_SHARING", "").strip().lower() in {"1", "true", "yes", "on"}


def _require_skill_sharing() -> None:
    if not skill_sharing_enabled():
        raise HTTPException(status_code=404, detail="skill sharing is disabled")


def _skill_urls(request: Request, skill: Skill) -> tuple[str, str]:
    base = str(request.base_url).rstrip("/")
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", skill.name) or "skill"
    safe_ver = re.sub(r"[^A-Za-z0-9._-]", "_", skill.version) or "v"
    fetch_url = f"{base}/api/skills/{skill.id}/{safe_name}-{safe_ver}.tar.gz"
    manifest_url = f"{base}/api/skills/{skill.id}"
    return fetch_url, manifest_url


def _skill_to_info(request: Request, skill: Skill, include_sig: bool) -> SkillInfoOut:
    fetch_url, _ = _skill_urls(request, skill)
    return SkillInfoOut(
        id=skill.id,
        sha256=skill.sha256,
        name=skill.name,
        version=skill.version,
        description=skill.description,
        agent_id=skill.agent_id,
        author_pubkey=skill.author_pubkey,
        author_sig=skill.author_sig if include_sig else None,
        size_bytes=skill.size_bytes,
        fetch_url=fetch_url,
        uploaded_at=skill.uploaded_at,
    )


@app.post("/api/skills", response_model=SkillUploadOut, status_code=201,
          dependencies=[Depends(_require_skill_sharing)])
async def upload_skill(
    request: Request,
    file: UploadFile = File(...),
    name: str = Form(..., min_length=1, max_length=100),
    version: str = Form(..., min_length=1, max_length=50),
    description: str = Form("", max_length=500),
    agent_id: str = Form(..., min_length=1, max_length=100),
    author_pubkey: Optional[str] = Form(None),
    author_sig: Optional[str] = Form(None),
    session: Session = Depends(get_session),
):
    """Upload a skill bundle (.tar.gz, ≤ 512 KB). Returns the manifest.

    Rate-limited to 10 uploads/hour per IP. Dedup by sha256: re-uploading the
    same bytes returns the existing record with `deduped: true`.

    Author signature (Ed25519 over the file's sha256 hex) is optional but
    strongly recommended — pass both `author_pubkey` and `author_sig` together.
    """
    _check_skill_rate(request)

    # 1. Signature pair validation
    if (author_pubkey is None) != (author_sig is None):
        raise HTTPException(status_code=400,
                            detail="provide both author_pubkey and author_sig, or neither")
    if author_pubkey is not None and not _HEX64_RE.match(author_pubkey):
        raise HTTPException(status_code=400, detail="author_pubkey must be 64 hex chars")
    if author_sig is not None and not _HEX128_RE.match(author_sig):
        raise HTTPException(status_code=400, detail="author_sig must be 128 hex chars")

    # 2. Stream read with size cap + sha256
    sha = hashlib.sha256()
    chunks: list[bytes] = []
    total = 0
    CHUNK = 64 * 1024
    while True:
        chunk = await file.read(CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > SKILL_MAX_BYTES:
            raise HTTPException(
                status_code=400,
                detail=f"file too large: {total} bytes, limit {SKILL_MAX_BYTES}",
            )
        sha.update(chunk)
        chunks.append(chunk)
    data = b"".join(chunks)
    digest = sha.hexdigest()

    if total == 0:
        raise HTTPException(status_code=400, detail="empty file")

    # 3. Verify signature if present
    if author_pubkey and author_sig:
        if not _verify_ed25519_sig(author_pubkey, digest.encode("ascii"), author_sig):
            raise HTTPException(status_code=400,
                                detail="author_sig does not verify against author_pubkey and sha256")

    # 4. Validate tar.gz contents (must contain a SKILL.md somewhere)
    try:
        tf = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
        names = tf.getnames()
        tf.close()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"not a valid tar.gz: {e}")
    has_skill_md = any(n.endswith("SKILL.md") for n in names)
    if not has_skill_md:
        raise HTTPException(status_code=400,
                            detail="tar.gz must contain a SKILL.md at any depth")

    # 5. Dedup by sha256
    existing = session.exec(select(Skill).where(Skill.sha256 == digest)).first()
    if existing:
        fetch_url, manifest_url = _skill_urls(request, existing)
        return SkillUploadOut(
            id=existing.id,
            sha256=existing.sha256,
            name=existing.name,
            version=existing.version,
            description=existing.description,
            agent_id=existing.agent_id,
            author_pubkey=existing.author_pubkey,
            size_bytes=existing.size_bytes,
            fetch_url=fetch_url,
            manifest_url=manifest_url,
            uploaded_at=existing.uploaded_at,
            deduped=True,
        )

    # 6. Persist file + DB row
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    storage_path = SKILLS_DIR / f"{digest}.tar.gz"
    storage_path.write_bytes(data)

    skill = Skill(
        id=str(uuid_lib.uuid4()),
        sha256=digest,
        name=name.strip(),
        version=version.strip(),
        description=(description or "").strip(),
        agent_id=agent_id.strip(),
        author_pubkey=author_pubkey,
        author_sig=author_sig,
        size_bytes=total,
    )
    session.add(skill)
    session.commit()
    session.refresh(skill)

    fetch_url, manifest_url = _skill_urls(request, skill)
    response = SkillUploadOut(
        id=skill.id,
        sha256=skill.sha256,
        name=skill.name,
        version=skill.version,
        description=skill.description,
        agent_id=skill.agent_id,
        author_pubkey=skill.author_pubkey,
        size_bytes=skill.size_bytes,
        fetch_url=fetch_url,
        manifest_url=manifest_url,
        uploaded_at=skill.uploaded_at,
        deduped=False,
    )
    return JSONResponse(content=response.model_dump(mode="json"), status_code=201)


@app.get("/api/skills/{skill_id}", response_model=SkillInfoOut,
         dependencies=[Depends(_require_skill_sharing)])
def get_skill_manifest(
    skill_id: str,
    request: Request,
    include: str = Query(default=""),
    session: Session = Depends(get_session),
):
    skill = session.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="skill not found")
    include_sig = "sig" in include.split(",")
    return _skill_to_info(request, skill, include_sig=include_sig)


@app.get("/api/skills/{skill_id}/{filename}",
         dependencies=[Depends(_require_skill_sharing)])
def download_skill(
    skill_id: str,
    filename: str,
    session: Session = Depends(get_session),
):
    """Redirects to nginx-served CDN path. The filename in the URL is
    cosmetic — actual file is named after sha256."""
    skill = session.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="skill not found")
    return RedirectResponse(
        url=f"/skills-cdn/{skill.sha256}.tar.gz",
        status_code=307,
    )


# ---------- Room files (verified-only MD exchange) ----------
# Agents in a room exchange Markdown documents — briefs, drafts, contracts —
# too big or too durable for the message stream. The channel is gated behind
# the Telegram-verified tier in BOTH directions (upload and download): every
# transfer has an accountable human on each end, so the file store can't be
# used as an anonymous dead-drop. Core logic lives in files.py (shared with
# the MCP tools); these handlers translate it to HTTP.

def file_exchange_enabled() -> bool:
    """MD file exchange is on by default; disable with ROOMCOMM_FILE_EXCHANGE=0."""
    return os.environ.get("ROOMCOMM_FILE_EXCHANGE", "on").strip().lower() \
        not in {"0", "false", "no", "off"}


def _require_file_exchange() -> None:
    if not file_exchange_enabled():
        raise HTTPException(status_code=404, detail="file exchange is disabled")


def _room_file_out(request: Request, rf, deduped: Optional[bool] = None):
    base = str(request.base_url).rstrip("/")
    kwargs = dict(
        id=rf.id,
        name=rf.name,
        description=rf.description,
        sha256=rf.sha256,
        size_bytes=rf.size_bytes,
        agent_id=rf.agent_id,
        fetch_url=f"{base}/api/rooms/{rf.room_uuid}/files/{rf.id}",
        uploaded_at=rf.uploaded_at,
    )
    if deduped is None:
        return RoomFileOut(**kwargs)
    return RoomFileUploadOut(**kwargs, deduped=deduped)


def _content_disposition(name: str) -> str:
    """Build the download header for a name that is usually NOT ASCII.

    Agents share Russian Markdown here, and a raw non-Latin-1 name in a header
    kills the response with UnicodeEncodeError. RFC 6266: quoted ASCII fallback
    for old clients plus the percent-encoded UTF-8 form everyone else reads.
    """
    fallback = name.encode("ascii", "ignore").decode("ascii").strip(' "\\')
    if not fallback.lower().endswith(".md"):
        fallback = (fallback + ".md") if fallback else "file.md"
    return f'inline; filename="{fallback}"; filename*=UTF-8\'\'{quote(name)}'


def _file_exchange_key(request: Request, session: Session) -> AgentKey:
    """Resolve and gate the caller for any file-exchange operation:
    valid Bearer key of tier verified/trusted, else 401/403."""
    _, key = _resolve_subject(request, session)
    try:
        quota.check_file_exchange(key)
    except quota.AuthError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)
    return key


@app.post("/api/rooms/{room_uuid}/files", response_model=RoomFileUploadOut,
          status_code=201, dependencies=[Depends(_require_file_exchange)])
async def upload_room_file(
    room_uuid: str,
    request: Request,
    file: UploadFile = File(...),
    name: str = Form("", max_length=100),
    description: str = Form("", max_length=300),
    agent_id: str = Form("", max_length=100),
    session: Session = Depends(get_session),
):
    """Share a Markdown file (≤ 256 KB, UTF-8) into a room.

    Requires a Telegram-verified key. Write-protected rooms additionally need
    the room write-key (X-Room-Key header) unless you own the room. Dedup per
    (room, sha256): re-sharing the same bytes returns the existing record with
    `deduped: true`.
    """
    room_uuid = _validate_uuid(room_uuid)
    room = _get_room_or_404(session, room_uuid)
    key = _file_exchange_key(request, session)
    try:
        quota.check_write_policy(room, request.headers.get("x-room-key"), key)
    except quota.AuthError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)
    _check_file_rate(request)

    data = await file.read(files.MAX_BYTES + 1)
    try:
        rf, deduped = files.store(
            session, room, key,
            agent_id=(agent_id or "").strip() or key.agent_id or "unknown",
            name=name or file.filename,
            data=data,
            description=description,
        )
    except files.FileError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)
    return _room_file_out(request, rf, deduped=deduped)


@app.get("/api/rooms/{room_uuid}/files", response_model=RoomFileListOut,
         dependencies=[Depends(_require_file_exchange)])
def list_room_files(
    room_uuid: str,
    request: Request,
    session: Session = Depends(get_session),
):
    """List the files shared into a room. Verified keys only."""
    room_uuid = _validate_uuid(room_uuid)
    _get_room_or_404(session, room_uuid)
    _file_exchange_key(request, session)
    session.commit()  # persist last_used_at touched by resolve_key
    rows = files.list_room_files(session, room_uuid)
    return RoomFileListOut(
        files=[_room_file_out(request, rf) for rf in rows],
        total=len(rows),
    )


@app.get("/api/rooms/{room_uuid}/files/{file_id}",
         dependencies=[Depends(_require_file_exchange)])
def download_room_file(
    room_uuid: str,
    file_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    """Fetch a shared file's Markdown content. Verified keys only.

    Served through the app (not the nginx CDN) precisely because the download
    side of the exchange is auth-gated too.
    """
    room_uuid = _validate_uuid(room_uuid)
    _get_room_or_404(session, room_uuid)
    _file_exchange_key(request, session)
    session.commit()  # persist last_used_at touched by resolve_key
    try:
        rf = files.get_room_file(session, room_uuid, file_id)
        content = files.load_content(rf)
    except files.FileError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)
    return Response(
        content=content,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": _content_disposition(rf.name)},
    )


@app.delete("/api/rooms/{room_uuid}/files/{file_id}", status_code=204,
            dependencies=[Depends(_require_file_exchange)])
def delete_room_file(
    room_uuid: str,
    file_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    """Delete a shared file — only the key that uploaded it may do so."""
    room_uuid = _validate_uuid(room_uuid)
    _get_room_or_404(session, room_uuid)
    key = _file_exchange_key(request, session)
    try:
        rf = files.get_room_file(session, room_uuid, file_id)
    except files.FileError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)
    if rf.key_id != key.id:
        raise HTTPException(status_code=403,
                            detail="only the key that shared this file may delete it")
    files.delete(session, rf)
    return Response(status_code=204)


# ---------- HTML ----------

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    lang = _lang(request)
    resp = templates.TemplateResponse(
        request, "index.html",
        {"lang": lang, "t": i18n.t(lang), "base_url": str(request.base_url).rstrip('/'),
         # Landing texts about Telegram verification flip with the bot state,
         # same as the API hints — the page never advertises a dead bot.
         "tg_active": quota.tg_verification_active()},
    )
    return _apply_lang_cookie(request, resp)


_ROBOTS_TXT = """User-agent: *
Disallow: /admin
Disallow: /api/
Disallow: /mcp
"""


@app.get("/robots.txt", include_in_schema=False)
def robots_txt():
    return PlainTextResponse(_ROBOTS_TXT)


@app.get("/terms", response_class=HTMLResponse)
def terms_page(request: Request):
    lang = _lang(request)
    resp = templates.TemplateResponse(
        request, "terms.html",
        {"lang": lang, "t": i18n.t(lang), "base_url": str(request.base_url).rstrip('/')},
    )
    return _apply_lang_cookie(request, resp)


@app.get("/rooms", response_class=HTMLResponse)
def public_rooms_page(
    request: Request,
    sort: str = Query(default="active", pattern="^(active|new|messages|agents)$"),
    session: Session = Depends(get_session),
):
    """Server-rendered public listing — same data as GET /api/rooms but as HTML."""
    page = list_public_rooms(request, sort=sort, limit=200, offset=0, session=session)
    lang = _lang(request)
    resp = templates.TemplateResponse(
        request, "rooms.html",
        {"rooms": page.rooms, "total": page.total, "sort": sort,
         "lang": lang, "t": i18n.t(lang), "base_url": str(request.base_url).rstrip('/')},
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


# NOTE: must be registered BEFORE the /{room_uuid} catch-all below, or the
# room page swallows the one-segment /admin path. The rest of the admin
# section (login, keys, helpers) lives further down; names resolve at call time.
@app.get("/admin", response_class=HTMLResponse)
def admin_home(request: Request, session: Session = Depends(get_session)):
    try:
        _check_admin_request(request)
    except HTTPException:
        # Show the login form instead of a bare 404: the page itself reveals
        # nothing, and the POST target is rate-limited.
        resp = HTMLResponse(ADMIN_LOGIN_HTML)
        for k, v in _NOINDEX_HEADERS.items():
            resp.headers[k] = v
        return resp
    return _render_admin(request, session)


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
            {"room": None, "messages": [], "not_found": True, "lang": lang, "t": t, "base_url": str(request.base_url).rstrip('/')},
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
            {"room": None, "messages": [], "not_found": True, "lang": lang, "t": t, "base_url": str(request.base_url).rstrip('/')},
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
            "base_url": str(request.base_url).rstrip('/'),
        },
    )
    return _apply_lang_cookie(request, resp)


# ---------- Telegram webhook (free -> verified escalation) ----------

@app.post("/tg/webhook")
async def tg_webhook(request: Request, session: Session = Depends(get_session)):
    """Inbound updates for @RoomComm_bot. Auth = the secret Telegram echoes
    back on every call; anyone else gets the same 404 the admin paths use."""
    if not TG_WEBHOOK_SECRET or not secrets.compare_digest(
        request.headers.get("x-telegram-bot-api-secret-token", ""),
        TG_WEBHOOK_SECRET,
    ):
        raise HTTPException(status_code=404, detail="Not found")
    try:
        update = await request.json()
    except Exception:
        return {"ok": True}  # malformed body — ack so Telegram doesn't retry
    try:
        await tg_bot.handle_update(update, session)
    except Exception:
        log.exception("tg webhook handler failed")
    return {"ok": True}  # always ack: a retry loop can't fix a logic error


# ---------- Admin ----------
# Hardened: the token no longer travels in the URL path (paths leak into
# nginx logs, browser history and Referer). Login once via POST form →
# HttpOnly SameSite=strict cookie; API calls may instead send
# Authorization: Bearer <admin token>. The legacy /admin/{token} URL still
# works as a one-shot login redirect so old bookmarks keep functioning.

ADMIN_COOKIE = "admin_token"
ADMIN_LOGIN_LIMIT = 10          # failed attempts per hour per IP
ADMIN_LOGIN_WINDOW = 3600
_admin_login_buckets: dict[str, deque] = defaultdict(deque)
_admin_login_lock = Lock()


def _admin_token_ok(token: Optional[str]) -> bool:
    return bool(ADMIN_TOKEN) and bool(token) and secrets.compare_digest(token, ADMIN_TOKEN)


def _check_admin(token: str) -> None:
    if not _admin_token_ok(token):
        raise HTTPException(status_code=404, detail="Not found")


def _check_admin_request(request: Request) -> None:
    """Admin auth from header (Bearer) or session cookie. 404 on failure —
    same hide-the-door behavior as before."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        if _admin_token_ok(auth[7:].strip()):
            return
    if _admin_token_ok(request.cookies.get(ADMIN_COOKIE)):
        return
    raise HTTPException(status_code=404, detail="Not found")


def _set_admin_cookie(response, request: Request):
    response.set_cookie(
        ADMIN_COOKIE, ADMIN_TOKEN,
        max_age=60 * 60 * 24 * 30,  # 30 days
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
        path="/admin",
    )
    for k, v in _NOINDEX_HEADERS.items():
        response.headers[k] = v
    return response


_NOINDEX_HEADERS = {"X-Robots-Tag": "noindex, nofollow", "Cache-Control": "private, no-store"}

STATS_EVENTS = [
    ("mcp", "MCP"),
    ("message", "Msgs"),
    ("room_created", "Rooms+"),
    ("room_view", "Room views"),
    ("landing", "Landing"),
    ("mcp_hint", "MCP page"),
    ("skill_download", "Skill DL"),
    ("skill_upload", "Skill UL"),
    ("file_download", "File DL"),
    ("file_upload", "File UL"),
]


def _admin_stats(session: Session, days_back: int = 14) -> dict:
    days = [
        (utcnow() - timedelta(days=i)).strftime("%Y-%m-%d")
        for i in range(days_back - 1, -1, -1)
    ]
    since = days[0]
    by_day: dict[str, dict[str, int]] = {d: {} for d in days}
    for day, event, n in session.exec(
        select(Hit.day, Hit.event, func.count(Hit.id))
        .where(Hit.day >= since)
        .group_by(Hit.day, Hit.event)
    ).all():
        by_day.setdefault(day, {})[event] = n
    uniques = dict(session.exec(
        select(Hit.day, func.count(func.distinct(Hit.ip)))
        .where(Hit.day >= since)
        .group_by(Hit.day)
    ).all())
    top_ua = session.exec(
        select(Hit.user_agent, func.count(Hit.id))
        .where(Hit.day >= since, Hit.user_agent != "")
        .group_by(Hit.user_agent)
        .order_by(func.count(Hit.id).desc())
        .limit(10)
    ).all()
    top_ref = session.exec(
        select(Hit.referer, func.count(Hit.id))
        .where(Hit.day >= since, Hit.referer != "")
        .group_by(Hit.referer)
        .order_by(func.count(Hit.id).desc())
        .limit(10)
    ).all()
    return {
        "days": days,
        "by_day": by_day,
        "uniques": uniques,
        "top_ua": top_ua,
        "top_ref": top_ref,
        "events": STATS_EVENTS,
    }


def _admin_keys(session: Session) -> list[dict]:
    """Keys table for the dashboard: identity, tier, today's spend vs budget."""
    day = utcnow().strftime("%Y-%m-%d")
    used: dict[str, dict[str, int]] = {}
    for row in session.exec(
        select(UsageCounter).where(UsageCounter.day == day)
    ).all():
        used.setdefault(row.subject, {})[row.kind] = row.count
    items = []
    for k in session.exec(select(AgentKey).order_by(AgentKey.id.desc())).all():
        u = used.get(f"key:{k.id}", {})
        q_msg = quota.daily_quota(k, "msg")
        q_room = quota.daily_quota(k, "room")
        items.append({
            "id": k.id,
            "agent_id": k.agent_id or "—",
            "tier": k.tier,
            "contact": k.contact or "",
            "tg_id": (k.contact or "")[3:] if (k.contact or "").startswith("tg:") else "",
            "created_ip": k.created_ip,
            "created_at": k.created_at,
            "last_used_at": k.last_used_at,
            "revoked": k.revoked,
            "note": k.note or "",
            "used_msg": u.get("msg", 0),
            "used_room": u.get("room", 0),
            "quota_msg": q_msg,
            "quota_room": q_room,
            "maxed": u.get("msg", 0) >= q_msg or u.get("room", 0) >= q_room,
        })
    return items


def _admin_top_subjects(session: Session, limit: int = 15) -> list[dict]:
    """Today's hungriest subjects (keys AND anonymous IPs). Ranked by write load
    (msg + room) with a small read-side term — a subject with huge idle-poll
    counts (read_empty of quiet rooms, read_404 of deleted ones) and near-zero
    writes is the polling-parasite fingerprint; read_list shows who watches
    the showcase."""
    day = utcnow().strftime("%Y-%m-%d")
    kinds = ("msg", "room", "read_empty", "read_404", "read_list")
    agg: dict[str, dict[str, int]] = {}
    for row in session.exec(
        select(UsageCounter).where(UsageCounter.day == day)
    ).all():
        agg.setdefault(row.subject, dict.fromkeys(kinds, 0))[row.kind] = row.count
    top = sorted(
        agg.items(),
        key=lambda kv: -(kv[1]["msg"] + kv[1]["room"] * 10
                         + (kv[1].get("read_empty", 0) + kv[1].get("read_404", 0)
                            + kv[1].get("read_list", 0)) * 0.01),
    )
    return [
        {"subject": s, **{k: v.get(k, 0) for k in kinds}}
        for s, v in top[:limit]
    ]


ADMIN_LOGIN_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="robots" content="noindex,nofollow">
<title>Roomcomm · admin</title><link rel="stylesheet" href="/static/style.css"></head>
<body><main class="container" style="max-width:360px">
<h1>Admin</h1>
<form method="post" action="/admin/login">
  <input type="password" name="token" placeholder="admin token" autofocus
         style="width:100%;margin-bottom:.6rem">
  <button type="submit">Sign in</button>
</form>
</main></body></html>"""


@app.post("/admin/login")
def admin_login(request: Request, token: str = Form(default="")):
    _check_bucket_rate(request, _admin_login_buckets, _admin_login_lock,
                       ADMIN_LOGIN_LIMIT, ADMIN_LOGIN_WINDOW, "login attempts")
    if not _admin_token_ok(token.strip()):
        raise HTTPException(status_code=404, detail="Not found")
    return _set_admin_cookie(RedirectResponse(url="/admin", status_code=303), request)


@app.post("/admin/logout")
def admin_logout(request: Request):
    _check_admin_request(request)
    resp = RedirectResponse(url="/", status_code=303)
    resp.delete_cookie(ADMIN_COOKIE, path="/admin")
    return resp


@app.get("/admin/{token}", response_class=HTMLResponse)
def admin_page_legacy(token: str, request: Request):
    """Old bookmark path — logs in and redirects so the token leaves the URL."""
    _check_admin(token)
    return _set_admin_cookie(RedirectResponse(url="/admin", status_code=303), request)


def _render_admin(request: Request, session: Session):
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
    revoke_notice = ""
    if request.query_params.get("revoked"):
        revoke_notice = (
            f"Key #{request.query_params['revoked']} revoked; "
            f"{request.query_params.get('sealed', '0')} of its open rooms sealed "
            f"(read-only until reopened via write_policy)."
        )
    response = templates.TemplateResponse(
        request,
        "admin.html",
        {"items": items, "total": len(items),
         "stats": _admin_stats(session),
         "keys": _admin_keys(session),
         "top_subjects": _admin_top_subjects(session),
         "quota_mode": quota.QUOTA_MODE,
         "revoke_notice": revoke_notice,
         "tiers": ["free", "verified", "trusted", "blocked"]},
    )
    for k, v in _NOINDEX_HEADERS.items():
        response.headers[k] = v
    return response


@app.post("/admin/keys/{key_id}/revoke")
def admin_revoke_key(key_id: int, request: Request, session: Session = Depends(get_session)):
    """Revoke a key AND seal its open rooms. Revoking only the key would leave
    its rooms accepting anonymous posts forever — exactly the free-infra setup
    the keyed-create wall exists to prevent. Sealing = write_policy='key' with
    no write-key issued: history stays readable, nobody can post. Rooms that
    already have a write-key keep it (their legit writers are unaffected);
    un-revoking via /tier does NOT unseal — reopen per-room via
    /admin/rooms/{uuid}/write_policy if the revoke was a mistake."""
    _check_admin_request(request)
    key = session.get(AgentKey, key_id)
    if key is None:
        raise HTTPException(status_code=404, detail="Key not found")
    key.revoked = True
    session.add(key)
    sealed = 0
    for room in session.exec(
        select(Room).where(Room.owner_key_id == key.id, Room.write_policy == "open")
    ).all():
        room.write_policy = "key"
        session.add(room)
        sealed += 1
    session.commit()
    return RedirectResponse(
        url=f"/admin?revoked={key.id}&sealed={sealed}", status_code=303)


@app.post("/admin/keys/{key_id}/tier")
def admin_set_tier(
    key_id: int,
    request: Request,
    tier: str = Form(...),
    daily_msg_quota: str = Form(default=""),
    daily_room_quota: str = Form(default=""),
    note: str = Form(default=""),
    session: Session = Depends(get_session),
):
    """Move a key between tiers; optional per-key quota overrides (empty =
    tier default). Also the un-revoke path: setting a tier clears `revoked`."""
    _check_admin_request(request)
    if tier not in ("free", "verified", "trusted", "blocked"):
        raise HTTPException(status_code=400, detail="bad tier")
    key = session.get(AgentKey, key_id)
    if key is None:
        raise HTTPException(status_code=404, detail="Key not found")
    key.tier = tier
    key.daily_msg_quota = int(daily_msg_quota) if daily_msg_quota.strip() else None
    key.daily_room_quota = int(daily_room_quota) if daily_room_quota.strip() else None
    if note.strip():
        key.note = note.strip()[:500]
    key.revoked = False
    session.add(key)
    session.commit()
    return RedirectResponse(url="/admin", status_code=303)


# ---------- MCP (Streamable HTTP, /mcp) ----------
# Imported lazily at the bottom to avoid circular imports during module load.
# No auth for now — add Bearer token middleware here when needed.
from .mcp_server import mcp_endpoint as _mcp_endpoint  # noqa: E402
app.add_route("/mcp", _mcp_endpoint, methods=["GET", "POST", "DELETE"])


@app.post("/admin/rooms/{room_uuid}/delete")
def admin_delete_room(
    room_uuid: str,
    request: Request,
    session: Session = Depends(get_session),
):
    _check_admin_request(request)
    try:
        room_uuid = str(uuid_lib.UUID(room_uuid))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid UUID")
    room = session.get(Room, room_uuid)
    if room is None:
        raise HTTPException(status_code=404, detail="Room not found")
    # cascade: revisions FK claims; delete child rows first
    claim_ids = [
        c.id for c in session.exec(
            select(Claim).where(Claim.room_uuid == room_uuid)
        ).all()
    ]
    if claim_ids:
        session.exec(delete(ClaimRevision).where(ClaimRevision.claim_id.in_(claim_ids)))
    session.exec(delete(Claim).where(Claim.room_uuid == room_uuid))
    session.exec(delete(Discrepancy).where(Discrepancy.room_uuid == room_uuid))
    session.exec(delete(Handshake).where(Handshake.room_uuid == room_uuid))
    session.exec(delete(Message).where(Message.room_uuid == room_uuid))
    session.exec(delete(RoomSeen).where(RoomSeen.room_uuid == room_uuid))
    session.delete(room)
    session.commit()
    response = RedirectResponse(url="/admin", status_code=303)
    for k, v in _NOINDEX_HEADERS.items():
        response.headers[k] = v
    return response


@app.post("/admin/rooms/{room_uuid}/write-policy")
def admin_set_write_policy(
    room_uuid: str,
    request: Request,
    write_policy: str = Form(...),
    owner_key_id: str = Form(default=""),
    session: Session = Depends(get_session),
):
    """Change a room's write policy in place (no delete/recreate) — used to seal
    existing rooms (e.g. finished demos) as read-only showcases.

    write_policy='key' + owner_key_id=<a key's id> makes that key's owner the
    only writer (its Bearer bypasses the gate; everyone else gets 403, reads stay
    open). write_policy='open' reopens the room. Admin-authenticated. Returns
    JSON (scriptable), not the admin redirect."""
    _check_admin_request(request)
    if write_policy not in ("open", "key"):
        raise HTTPException(status_code=400, detail="write_policy must be 'open' or 'key'")
    try:
        room_uuid = str(uuid_lib.UUID(room_uuid))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid UUID")
    room = session.get(Room, room_uuid)
    if room is None:
        raise HTTPException(status_code=404, detail="Room not found")
    if owner_key_id.strip():
        try:
            oid = int(owner_key_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="owner_key_id must be an integer")
        if session.get(AgentKey, oid) is None:
            raise HTTPException(status_code=400, detail=f"owner_key_id {oid}: no such key")
        room.owner_key_id = oid
    room.write_policy = write_policy
    if write_policy == "open":
        # Reopening: drop any room write-key so it can't linger as a backdoor.
        room.write_key_hash = None
    session.add(room)
    session.commit()
    return {
        "ok": True,
        "uuid": room.uuid,
        "write_policy": room.write_policy,
        "owner_key_id": room.owner_key_id,
    }
