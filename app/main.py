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
from pathlib import Path
from threading import Lock
from typing import Optional

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
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

# Skill upload limits — same shape as room create, independent bucket.
SKILL_UPLOAD_LIMIT = 10
SKILL_UPLOAD_WINDOW = 3600
SKILL_MAX_BYTES = 512 * 1024
_skill_buckets: dict[str, deque] = defaultdict(deque)
_skill_lock = Lock()
_HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_HEX128_RE = re.compile(r"^[0-9a-fA-F]{128}$")


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


def _check_skill_rate(request: Request) -> None:
    """Raise 429 if the IP has uploaded too many skills in the last hour."""
    ip = _client_ip(request)
    now = time.monotonic()
    with _skill_lock:
        bucket = _skill_buckets[ip]
        cutoff = now - SKILL_UPLOAD_WINDOW
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= SKILL_UPLOAD_LIMIT:
            retry_after = int(SKILL_UPLOAD_WINDOW - (now - bucket[0])) + 1
            raise HTTPException(
                status_code=429,
                detail=f"Too many skill uploads from this IP. Try again in {retry_after}s.",
                headers={"Retry-After": str(retry_after)},
            )
        bucket.append(now)


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

from . import i18n, llm, notify, pcis
from .database import SKILLS_DIR, engine, get_session, init_db
from .models import Claim, ClaimRevision, Discrepancy, Handshake, Message, Room, Skill
from .schemas import (
    ClaimIn,
    ContextOut,
    DiscrepancyOut,
    HandshakeIn,
    HandshakeOut,
    MessageIn,
    MessageOut,
    MessagesPage,
    RefreshOut,
    RevisionIn,
    RevisionOut,
    RoomCreate,
    RoomCreateOut,
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
    async with _mcp_lifespan(app_):
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


# ---------- API ----------

@app.post("/api/rooms", response_model=RoomCreateOut, status_code=201)
def create_room(
    payload: RoomCreate,
    request: Request,
    background_tasks: BackgroundTasks,
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
        protocol_mode=payload.protocol_mode,
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
        protocol_mode=room.protocol_mode,
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
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
):
    room_uuid = _validate_uuid(room_uuid)
    room = _get_room_or_404(session, room_uuid)
    count = session.exec(
        select(func.count()).select_from(Message).where(Message.room_uuid == room_uuid)
    ).one()
    if count >= MAX_MESSAGES_PER_ROOM:
        raise HTTPException(status_code=429, detail="Room message limit reached (1000)")

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
    )
    # If signed, lock the message timestamp to the agent's ts_iso so the
    # signed surface remains reproducible. Otherwise the default factory
    # assigns now().
    if sig_provided and ts_iso:
        from datetime import datetime as _dt
        msg.timestamp = _dt.fromisoformat(ts_iso.replace("Z", "+00:00"))
    session.add(msg)
    session.commit()
    session.refresh(msg)

    # Premium rooms: schedule async LLM extraction after response goes out.
    if room.protocol_mode == "premium" and llm.is_configured():
        background_tasks.add_task(_refresh_room_context_bg, room_uuid)

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
        }

    new_threads = 0
    revisions = 0
    discs = 0
    last_model_used = "noop"

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

    elapsed_ms = int((time.monotonic() - started) * 1000)
    return {
        "processed_msgs": len(new_msgs), "new_threads": new_threads,
        "revisions": revisions, "discrepancies": discs,
        "model_used": last_model_used, "elapsed_ms": elapsed_ms,
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


@app.post("/api/skills", response_model=SkillUploadOut, status_code=201)
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


@app.get("/api/skills/{skill_id}", response_model=SkillInfoOut)
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


@app.get("/api/skills/{skill_id}/{filename}")
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


# ---------- HTML ----------

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    lang = _lang(request)
    resp = templates.TemplateResponse(
        request, "index.html",
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


# ---------- MCP (Streamable HTTP, /mcp) ----------
# Imported lazily at the bottom to avoid circular imports during module load.
# No auth for now — add Bearer token middleware here when needed.
from .mcp_server import mcp_endpoint as _mcp_endpoint  # noqa: E402
app.add_route("/mcp", _mcp_endpoint, methods=["GET", "POST", "DELETE"])


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
    session.delete(room)
    session.commit()
    response = RedirectResponse(url=f"/admin/{token}", status_code=303)
    for k, v in _NOINDEX_HEADERS.items():
        response.headers[k] = v
    return response
