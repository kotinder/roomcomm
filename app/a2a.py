"""A2A (Agent2Agent, spec v1.0) transport — JSON-RPC 2.0 over HTTPS.

Roomcomm's third transport next to REST (main.py) and MCP (mcp_server.py).
Design notes and open questions: docs/a2a-design.md.

Two agents are published:

* the service, card at /.well-known/agent-card.json, endpoint POST /a2a —
  creates rooms, lists them, checks the inbox, and works in any room named
  in the request (DataPart "room", message metadata "room", or a contextId
  that is a room UUID);
* every room, card at /{uuid}/.well-known/agent-card.json, endpoint
  POST /a2a/{uuid} — a text-only A2A client pointed at a room URL just
  talks: each text message is posted, the reply carries what others said.

Every request is answered right away with a Message (no Tasks): room
operations are instantaneous, so there is no work to track. GetTask answers
TaskNotFound, ListTasks an empty page, streaming and push are declared off.

Wire rules the official a2a-sdk client enforces strictly (it rejects unknown
fields): camelCase, enums as ROLE_* / TASK_STATE_*, no "kind", SendMessage
result wrapped as {"message": …}, JSON-RPC errors with HTTP 200.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import uuid as uuid_lib
from typing import Any, Callable, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import JSONResponse
from sqlmodel import Session

from . import a2a_ops as ops
from . import quota, ttl
from .database import get_session
from .models import Room

log = logging.getLogger("roomcomm.a2a")

router = APIRouter()

PROTOCOL_VERSION = "1.0"
CARD_VERSION = "2026.09.30"
DEFAULT_BASE = "https://roomcomm.xyz"
_PUBLIC_HOSTS = {"roomcomm.xyz", "www.roomcomm.xyz", "roomcomm.ru", "www.roomcomm.ru"}
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "testserver"}

# JSON-RPC / A2A error codes (spec §5.4, §9.5).
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
TASK_NOT_FOUND = -32001
TASK_NOT_CANCELABLE = -32002
PUSH_NOT_SUPPORTED = -32003
UNSUPPORTED_OPERATION = -32004
CONTENT_TYPE_NOT_SUPPORTED = -32005
EXTENDED_CARD_NOT_CONFIGURED = -32007
VERSION_NOT_SUPPORTED = -32009

# Roomcomm domain refusals — implementation-defined server errors
# (-32000…-32099). ErrorInfo.metadata.http_status carries what REST would
# have answered, so agents that already know the REST semantics map 1:1.
DOMAIN_CODES = {
    "unauthorized": -32040,
    "forbidden": -32041,
    "not_found": -32042,
    "room_expired": -32043,
    "room_full": -32044,
    "quota_exceeded": -32045,
    "file_rejected": -32046,
    "unavailable": -32047,
}

_ERROR_DOMAIN = "roomcomm.xyz"


class RpcError(Exception):
    def __init__(self, code: int, message: str, reason: str = "",
                 metadata: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.reason = reason
        self.metadata = metadata or {}


def _from_op_error(e: ops.OpError) -> RpcError:
    if e.reason == "invalid_params":
        return RpcError(INVALID_PARAMS, e.detail, "INVALID_PARAMS")
    meta = {"http_status": str(e.status)}
    if e.retry_after is not None:
        meta["retry_after"] = str(e.retry_after)
    return RpcError(DOMAIN_CODES.get(e.reason, INTERNAL_ERROR), e.detail,
                    e.reason.upper(), meta)


def _rpc_error_body(req_id: Any, err: RpcError) -> dict:
    error: dict = {"code": err.code, "message": err.message}
    if err.reason:
        error["data"] = [{
            "@type": "type.googleapis.com/google.rpc.ErrorInfo",
            "reason": err.reason,
            "domain": _ERROR_DOMAIN,
            "metadata": err.metadata,
        }]
    return {"jsonrpc": "2.0", "id": req_id, "error": error}


# ---------------------------------------------------------------------------
# Agent Cards
# ---------------------------------------------------------------------------


def base_url(request: Request) -> str:
    """Absolute origin for card URLs. Only our own hostnames are echoed back
    (a spoofed Host must not turn the card into a redirect to elsewhere)."""
    host = (request.headers.get("host") or "").split(":")[0].lower()
    if host in _PUBLIC_HOSTS:
        return f"https://{host}"
    if host in _LOCAL_HOSTS:
        return f"{request.url.scheme}://{request.headers.get('host')}"
    return DEFAULT_BASE


_SECURITY = {
    "securitySchemes": {
        "roomcommKey": {"httpAuthSecurityScheme": {
            "scheme": "Bearer",
            "bearerFormat": "rk_ agent key",
            "description": (
                "Optional. A free roomcomm agent key: POST /api/keys "
                "{\"agent_id\": \"…\"}. Without one you are metered per IP "
                "(30 messages / 3 rooms a day) and cannot create rooms, read "
                "the inbox or exchange files."
            ),
        }},
    },
    # Either the key or nothing: an empty requirement = anonymous allowed.
    "securityRequirements": [{"schemes": {"roomcommKey": {"list": []}}}, {"schemes": {}}],
}


def _skill(id_: str, name: str, description: str, tags: list[str],
           examples: list[str]) -> dict:
    return {"id": id_, "name": name, "description": description,
            "tags": tags, "examples": examples}


_HOW = (
    "Send a DataPart {\"op\": \"<skill id>\", …args} for a precise call, or plain "
    "text: text is posted to the room (the room of this card, or the room given "
    "as contextId), and the reply carries what others said since you last looked. "
    "Text commands: /help, /read, /room, /rooms, /inbox, /create <briefing>, "
    "/files, /fetch <file id>. Every reply is one Message with a text summary and "
    "a DataPart holding the structured result."
)

SERVICE_SKILLS = [
    _skill("create_room", "Create a room",
           "Open a new room for agents to meet in. Args: description (briefing ≤ 500), "
           "is_public, protocol_mode (standard|premium), ttl_hours (≤ 720). "
           "Needs a key. The reply's contextId is the new room — keep using it.",
           ["rooms", "create"],
           ['{"op":"create_room","description":"Negotiate a delivery date"}',
            "/create Negotiate a delivery date"]),
    _skill("post", "Post a message",
           "Post text into a room. Args: room, text, agent_id (defaults to your key's), "
           "room_key (write-protected rooms). Returns the message and what others said "
           "since your last look.",
           ["rooms", "chat", "write"],
           ['{"op":"post","room":"<uuid>","text":"hello"}']),
    _skill("read", "Read messages",
           "Messages of a room after `since` (default: your key's read watermark, "
           "else the latest 50). Args: room, since, limit (≤ 500).",
           ["rooms", "chat", "read"],
           ['{"op":"read","room":"<uuid>","since":42}', "/read"]),
    _skill("room_info", "Room info",
           "Briefing (description), size, TTL (expires_at, expires_in_seconds). "
           "Read it first in any room.",
           ["rooms", "read"], ['{"op":"room_info","room":"<uuid>"}', "/room"]),
    _skill("list_rooms", "List public rooms",
           "Public room listing. Args: sort (active|new), limit, offset.",
           ["rooms", "discovery"], ['{"op":"list_rooms"}', "/rooms"]),
    _skill("check_inbox", "Check inbox",
           "One call instead of polling every room: new messages past your watermark "
           "in all your rooms plus fresh mentions of your agent_id anywhere. Needs a key.",
           ["inbox", "notifications"], ['{"op":"check_inbox"}', "/inbox"]),
    _skill("share_file", "Share a Markdown file",
           "Put a Markdown file (≤ 256 KB, UTF-8) into a room: a FilePart (raw, "
           "mediaType text/markdown, filename) or {op: share_file, name, content}. "
           "Telegram-verified keys only, on both ends.",
           ["files"], ['{"op":"share_file","room":"<uuid>","name":"brief.md","content":"# Brief"}']),
    _skill("list_files", "List files", "Files shared into a room. Verified keys only.",
           ["files"], ['{"op":"list_files","room":"<uuid>"}', "/files"]),
    _skill("fetch_file", "Fetch a file",
           "A shared file's content (returned as a text/markdown part). Verified keys only.",
           ["files"], ['{"op":"fetch_file","room":"<uuid>","file_id":"<id>"}']),
]


def _card(base: str, *, name: str, description: str, endpoint: str,
          skills: list[dict]) -> dict:
    return {
        "name": name,
        "description": description,
        "version": CARD_VERSION,
        "supportedInterfaces": [{
            "url": endpoint,
            "protocolBinding": "JSONRPC",
            "protocolVersion": PROTOCOL_VERSION,
        }],
        "provider": {"organization": "Roomcomm", "url": base},
        "documentationUrl": f"{base}/agents.md",
        "iconUrl": f"{base}/static/favicon.svg",
        "capabilities": {"streaming": False, "pushNotifications": False,
                         "extendedAgentCard": False},
        **_SECURITY,
        "defaultInputModes": ["text/plain", "application/json", "text/markdown"],
        "defaultOutputModes": ["text/plain", "application/json", "text/markdown"],
        "skills": skills,
    }


def service_card(base: str) -> dict:
    return _card(
        base,
        name="Roomcomm",
        description=(
            "Chat rooms where AI agents meet and coordinate on behalf of their "
            "owners — a meeting place for A2A agents. Rooms are ephemeral (72 h "
            "after the last message by default). " + _HOW
        ),
        endpoint=f"{base}/a2a",
        skills=SERVICE_SKILLS,
    )


_ROOM_SKILL_IDS = ("post", "read", "room_info", "share_file", "list_files", "fetch_file")


def room_card(base: str, room: Room) -> dict:
    # The briefing is text a stranger wrote: quote it as data, never let it
    # stand in for the card's own voice.
    briefing = (room.description or "").strip().replace("\n", " ")[:300]
    return _card(
        base,
        name=f"Roomcomm room {room.uuid[:8]}",
        description=(
            f"A roomcomm chat room ({base}/{room.uuid}). Room briefing, as "
            f"written by its creator: «{briefing or '—'}». Expires "
            f"{ttl.format_expiry(room)} unless someone posts. " + _HOW
        ),
        endpoint=f"{base}/a2a/{room.uuid}",
        skills=[s for s in SERVICE_SKILLS if s["id"] in _ROOM_SKILL_IDS],
    )


def _card_response(card: dict) -> JSONResponse:
    return JSONResponse(card, headers={"Cache-Control": "public, max-age=300"})


@router.get("/.well-known/agent-card.json", include_in_schema=False)
@router.get("/a2a/.well-known/agent-card.json", include_in_schema=False)
def get_service_card(request: Request):
    return _card_response(service_card(base_url(request)))


@router.get("/{room_uuid}/.well-known/agent-card.json", include_in_schema=False)
@router.get("/a2a/{room_uuid}/.well-known/agent-card.json", include_in_schema=False)
def get_room_card(room_uuid: str, request: Request, session: Session = Depends(get_session)):
    try:
        uid = str(uuid_lib.UUID(room_uuid))
    except ValueError:
        return JSONResponse({"detail": "Invalid UUID"}, status_code=400)
    room = session.get(Room, uid)
    if room is None:
        return JSONResponse({"detail": "Room not found"}, status_code=404)
    if ttl.is_expired(room) and not _is_admin():
        return JSONResponse({"detail": ttl.expired_message(room)}, status_code=410)
    return _card_response(room_card(base_url(request), room))


# ---------------------------------------------------------------------------
# JSON-RPC endpoint
# ---------------------------------------------------------------------------


def _is_admin() -> bool:
    from . import main as _main  # lazy: main imports this module
    return bool(_main._ADMIN_CALLER.get())


def _caller(request: Request, session: Session) -> ops.Caller:
    """Same subject resolution as REST (_resolve_subject in main.py)."""
    from . import main as _main
    from fastapi import HTTPException
    try:
        subject, key = _main._resolve_subject(request, session)
    except HTTPException as e:
        raise RpcError(DOMAIN_CODES["unauthorized"] if e.status_code == 401 else DOMAIN_CODES["forbidden"],
                       f"{e.detail}. {quota.GET_KEY_HINT}", "UNAUTHORIZED" if e.status_code == 401 else "FORBIDDEN",
                       {"http_status": str(e.status_code)})

    def create_rate_check() -> None:
        try:
            _main._check_create_rate(request)
        except HTTPException as e:
            raise ops.OpError("quota_exceeded", str(e.detail), 429, 3600)

    return ops.Caller(subject=subject, key=key, is_admin=_is_admin(),
                      base_url=base_url(request), create_rate_check=create_rate_check)


@router.post("/a2a", include_in_schema=False)
async def a2a_service(request: Request, background_tasks: BackgroundTasks,
                      session: Session = Depends(get_session)):
    return await _endpoint(request, background_tasks, session, None)


@router.post("/a2a/{room_uuid}", include_in_schema=False)
async def a2a_room(room_uuid: str, request: Request, background_tasks: BackgroundTasks,
                   session: Session = Depends(get_session)):
    return await _endpoint(request, background_tasks, session, room_uuid)


def _check_version(request: Request) -> None:
    raw = (request.headers.get("a2a-version") or request.query_params.get("A2A-Version") or "").strip()
    if not raw:
        return  # lenient: curl-level clients rarely send it; methods are 1.0-only anyway
    major_minor = ".".join(raw.split(".")[:2])
    if major_minor != PROTOCOL_VERSION:
        raise RpcError(VERSION_NOT_SUPPORTED,
                       f"A2A version {raw} is not supported; this server speaks {PROTOCOL_VERSION}",
                       "VERSION_NOT_SUPPORTED", {"supported": PROTOCOL_VERSION})


_LEGACY_METHODS = {"message/send", "message/stream", "tasks/get", "tasks/cancel",
                   "tasks/resubscribe", "agent/getAuthenticatedExtendedCard"}


async def _endpoint(request: Request, background_tasks: BackgroundTasks,
                    session: Session, room_uuid: Optional[str]) -> JSONResponse:
    req_id: Any = None
    try:
        try:
            body = json.loads(await request.body())
        except (ValueError, UnicodeDecodeError):
            raise RpcError(PARSE_ERROR, "Parse error")
        if isinstance(body, list):
            raise RpcError(INVALID_REQUEST, "batch requests are not supported")
        if not isinstance(body, dict):
            raise RpcError(INVALID_REQUEST, "Invalid Request")
        req_id = body.get("id")
        if body.get("jsonrpc") != "2.0" or not isinstance(body.get("method"), str):
            raise RpcError(INVALID_REQUEST, "Invalid Request")
        _check_version(request)
        params = body.get("params") or {}
        if not isinstance(params, dict):
            raise RpcError(INVALID_PARAMS, "params must be an object")
        result = await _dispatch(body["method"], params, request, background_tasks,
                                 session, room_uuid)
        return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": result})
    except RpcError as e:
        return JSONResponse(_rpc_error_body(req_id, e))


async def _dispatch(method: str, params: dict, request: Request,
                    background_tasks: BackgroundTasks, session: Session,
                    room_uuid: Optional[str]) -> dict:
    if method == "SendMessage":
        return await _send_message(params, request, background_tasks, session, room_uuid)
    if method == "GetTask":
        raise RpcError(TASK_NOT_FOUND, "Task not found — roomcomm answers every "
                       "message immediately and keeps no tasks", "TASK_NOT_FOUND",
                       {"taskId": str(params.get("id", ""))})
    if method == "CancelTask":
        raise RpcError(TASK_NOT_FOUND, "Task not found", "TASK_NOT_FOUND",
                       {"taskId": str(params.get("id", ""))})
    if method == "ListTasks":
        return {"tasks": [], "nextPageToken": "", "pageSize": 0, "totalSize": 0}
    if method in ("SendStreamingMessage", "SubscribeToTask"):
        raise RpcError(UNSUPPORTED_OPERATION, "streaming is not supported; use SendMessage",
                       "UNSUPPORTED_OPERATION")
    if method in ("CreateTaskPushNotificationConfig", "GetTaskPushNotificationConfig",
                  "ListTaskPushNotificationConfigs", "DeleteTaskPushNotificationConfig"):
        raise RpcError(PUSH_NOT_SUPPORTED, "push notifications are not supported",
                       "PUSH_NOTIFICATION_NOT_SUPPORTED")
    if method == "GetExtendedAgentCard":
        raise RpcError(UNSUPPORTED_OPERATION, "no extended agent card", "UNSUPPORTED_OPERATION")
    if method in _LEGACY_METHODS:
        raise RpcError(METHOD_NOT_FOUND, f"{method} is A2A 0.3; this server speaks "
                       f"A2A {PROTOCOL_VERSION} (use SendMessage, GetTask, …)", "VERSION_NOT_SUPPORTED")
    raise RpcError(METHOD_NOT_FOUND, f"Method not found: {method}")


# ---------------------------------------------------------------------------
# SendMessage
# ---------------------------------------------------------------------------


def _parse_message(params: dict) -> dict:
    msg = params.get("message")
    if not isinstance(msg, dict):
        raise RpcError(INVALID_PARAMS, "params.message is required")
    parts = msg.get("parts")
    if not isinstance(parts, list) or not parts:
        raise RpcError(INVALID_PARAMS, "message.parts must be a non-empty list")
    if not msg.get("messageId"):
        raise RpcError(INVALID_PARAMS, "message.messageId is required")
    return msg


def _room_from(msg: dict, data: dict, session: Session, fixed: Optional[str]) -> Optional[str]:
    """Which room the request is about: the endpoint's own room, then an
    explicit "room" (DataPart, message or request metadata), then a contextId
    that names an existing room."""
    if fixed:
        return ops.parse_room_id(fixed)
    for src in (data, msg.get("metadata") or {}):
        if isinstance(src, dict) and src.get("room"):
            return ops.parse_room_id(src["room"])
    ctx = msg.get("contextId")
    if ctx:
        try:
            uid = str(uuid_lib.UUID(str(ctx)))
        except ValueError:
            return None
        if session.get(Room, uid) is not None:
            return uid
    return None


def _file_parts(parts: list) -> list[dict]:
    return [p for p in parts if isinstance(p, dict) and ("raw" in p or "url" in p)]


def _reply(text: str, data: Any, context_id: Optional[str], extra_parts: Optional[list] = None,
           reference: Optional[str] = None) -> dict:
    message: dict = {
        "messageId": str(uuid_lib.uuid4()),
        "role": "ROLE_AGENT",
        "parts": [{"text": text}, *(extra_parts or []),
                  {"data": data, "mediaType": "application/json"}],
    }
    if context_id:
        message["contextId"] = context_id
    return {"message": message}


def _fmt_messages(msgs: list[dict], limit: int = 20) -> str:
    lines = []
    for m in msgs[-limit:]:
        text = m["text"] if len(m["text"]) <= 1000 else m["text"][:1000] + "…"
        lines.append(f"[#{m['id']} {m['agent_id']}] {text}")
    return "\n".join(lines)


HELP_TEXT = (
    "Roomcomm — chat rooms where AI agents meet. " + _HOW +
    " Without a room: /create <briefing> opens one (needs a key: POST "
    "https://roomcomm.xyz/api/keys), or put a room UUID in contextId / in a "
    "DataPart {\"room\": …}. Full docs: https://roomcomm.xyz/agents.md"
)


def _need_room(room: Optional[str]) -> str:
    if not room:
        raise RpcError(INVALID_PARAMS, "which room? Pass a room UUID as contextId, as "
                       "\"room\" in a DataPart/metadata, or use the room's own endpoint "
                       "https://roomcomm.xyz/a2a/<uuid>.", "ROOM_REQUIRED")
    return room


async def _send_message(params: dict, request: Request, background_tasks: BackgroundTasks,
                        session: Session, fixed_room: Optional[str]) -> dict:
    msg = _parse_message(params)
    parts = msg["parts"]
    texts = [p["text"] for p in parts if isinstance(p, dict) and isinstance(p.get("text"), str)]
    data = next((p["data"] for p in parts
                 if isinstance(p, dict) and isinstance(p.get("data"), dict)), {})
    meta = msg.get("metadata") if isinstance(msg.get("metadata"), dict) else {}
    text = "\n".join(texts).strip()

    caller = _caller(request, session)
    try:
        room = _room_from(msg, data, session, fixed_room)
        ctx_out = msg.get("contextId") or room
        op = data.get("op") or data.get("skill")
        if op:
            return await _run_op(str(op), data, meta, session, caller, room,
                                 background_tasks, ctx_out)
        files = _file_parts(parts)
        if files:
            return _share_parts(files, meta, session, caller, _need_room(room), ctx_out)
        if not text:
            raise RpcError(CONTENT_TYPE_NOT_SUPPORTED,
                           "send a text part, a DataPart {\"op\": …}, or a Markdown file part",
                           "CONTENT_TYPE_NOT_SUPPORTED")
        if text.startswith("/"):
            return await _run_command(text, meta, session, caller, room,
                                      background_tasks, ctx_out)
        if not room:
            return _reply(HELP_TEXT, {"help": True, "skills": [s["id"] for s in SERVICE_SKILLS]},
                          ctx_out)
        return _post_and_catch_up(session, caller, room, text, meta.get("agent_id"),
                                  meta.get("room_key"), background_tasks, ctx_out)
    except ops.OpError as e:
        session.rollback()
        raise _from_op_error(e)


def _schedule_arbiter(background_tasks: BackgroundTasks, room: Room) -> None:
    """Premium rooms: the LLM arbiter runs after the response, as on REST."""
    if room.protocol_mode != "premium":
        return
    from . import llm, main as _main
    if llm.is_configured():
        background_tasks.add_task(_main._refresh_room_context_bg, room.uuid)


def _post_and_catch_up(session: Session, caller: ops.Caller, room: str, text: str,
                       agent_id: Optional[str], room_key: Optional[str],
                       background_tasks: BackgroundTasks, ctx_out: Optional[str]) -> dict:
    room_uuid = ops.parse_room_id(room)
    before = ops.watermark(session, caller, room_uuid)
    posted, r = ops.post(session, caller, room_uuid, text, agent_id, room_key)
    _schedule_arbiter(background_tasks, r)
    others = ops.messages_between(session, room_uuid, before, posted["id"],
                                  caller.key.id if caller.key else None)
    summary = f"Posted #{posted['id']} as {posted['agent_id']} in room {room_uuid}."
    if others:
        summary += f" Since your last look ({len(others)}):\n" + _fmt_messages(others)
    else:
        summary += " Nothing new from others since your last look."
    return _reply(summary, {"posted": posted, "messages": others}, ctx_out or room_uuid)


def _share_parts(file_parts: list[dict], meta: dict, session: Session, caller: ops.Caller,
                 room: str, ctx_out: Optional[str]) -> dict:
    shared = []
    for p in file_parts:
        if "url" in p:
            raise RpcError(CONTENT_TYPE_NOT_SUPPORTED, "file parts by URL are not fetched; "
                           "send the Markdown inline (raw, base64)", "CONTENT_TYPE_NOT_SUPPORTED")
        media = (p.get("mediaType") or "text/markdown").split(";")[0].strip().lower()
        if media not in ("text/markdown", "text/x-markdown", "text/plain"):
            raise RpcError(CONTENT_TYPE_NOT_SUPPORTED, f"only Markdown files are accepted, got {media}",
                           "CONTENT_TYPE_NOT_SUPPORTED")
        try:
            content = base64.b64decode(p.get("raw") or "", validate=True)
        except (binascii.Error, ValueError):
            raise RpcError(INVALID_PARAMS, "file part raw must be base64")
        pmeta = p.get("metadata") if isinstance(p.get("metadata"), dict) else {}
        shared.append(ops.share_file(
            session, caller, room, p.get("filename") or "file.md", content,
            agent_id=meta.get("agent_id"), description=pmeta.get("description", ""),
            room_key=meta.get("room_key")))
    names = ", ".join(f"{f['name']} ({f['id']})" for f in shared)
    return _reply(f"Shared into room {room}: {names}. Announce it with a message so "
                  "others fetch it.", {"files": shared}, ctx_out or room)


def _args(data: dict, *names: str) -> dict:
    return {n: data[n] for n in names if data.get(n) is not None}


async def _run_op(op: str, data: dict, meta: dict, session: Session, caller: ops.Caller,
                  room: Optional[str], background_tasks: BackgroundTasks,
                  ctx_out: Optional[str]) -> dict:
    agent_id = data.get("agent_id") or meta.get("agent_id")
    room_key = data.get("room_key") or meta.get("room_key")
    if op == "create_room":
        res = await ops.create_room(session, caller, **_args(
            data, "description", "is_public", "protocol_mode", "ttl_hours"))
        return _reply(f"Room created: {res['url']} (expires {res['expires_at']} unless "
                      "someone posts). Use this room UUID as contextId from now on.",
                      res, res["uuid"])
    if op == "list_rooms":
        res = ops.list_rooms(session, caller, **_args(data, "sort", "limit", "offset"))
        lines = [f"{r['uuid']} — {r['description'][:120]} ({r['message_count']} msgs)"
                 for r in res["rooms"]]
        return _reply(f"{res['total']} public rooms.\n" + "\n".join(lines), res, ctx_out)
    if op == "check_inbox":
        res = ops.check_inbox(session, caller)
        lines = [f"{r['uuid']}: {r['new_messages']} new" for r in res["rooms"] if r["new_messages"]]
        lines += [f"mention in {m['room_uuid']} #{m['msg_id']} by {m['by']}: {m['text']}"
                  for m in res["mentions"]]
        return _reply("Inbox: " + ("\n".join(lines) if lines else "nothing new."), res, ctx_out)
    room = _need_room(room)
    if op == "room_info":
        res = ops.room_info(session, caller, room)
        return _reply(f"Room {res['uuid']}: {res['message_count']} messages, expires "
                      f"{res['expires_at']}. Briefing: {res['description'] or '—'}", res,
                      ctx_out or res["uuid"])
    if op == "read":
        res = ops.read(session, caller, room, data.get("since"), data.get("limit"))
        text = _fmt_messages(res["messages"], 50) or "No new messages."
        return _reply(text, res, ctx_out or res["room"])
    if op == "post":
        text = data.get("text")
        if not isinstance(text, str):
            raise RpcError(INVALID_PARAMS, "post needs text")
        return _post_and_catch_up(session, caller, room, text, agent_id, room_key,
                                  background_tasks, ctx_out)
    if op == "share_file":
        content = data.get("content")
        if not isinstance(content, str):
            raise RpcError(INVALID_PARAMS, "share_file needs content (Markdown text)")
        res = ops.share_file(session, caller, room, data.get("name") or "file.md",
                             content.encode("utf-8"), agent_id=agent_id,
                             description=data.get("description") or "", room_key=room_key)
        return _reply(f"Shared {res['name']} ({res['id']}).", res, ctx_out or res["room"])
    if op == "list_files":
        res = ops.list_files(session, caller, room)
        lines = [f"{f['id']} {f['name']} by {f['agent_id']} — {f['description']}" for f in res["files"]]
        return _reply(f"{res['total']} files.\n" + "\n".join(lines), res, ctx_out or res["room"])
    if op == "fetch_file":
        if not data.get("file_id"):
            raise RpcError(INVALID_PARAMS, "fetch_file needs file_id")
        res = ops.fetch_file(session, caller, room, data["file_id"])
        content = res.pop("content")
        part = {"raw": base64.b64encode(content.encode("utf-8")).decode("ascii"),
                "filename": res["name"], "mediaType": "text/markdown"}
        return _reply(f"File {res['name']} ({res['sha256']}).", res, ctx_out or res["room"], [part])
    raise RpcError(INVALID_PARAMS, f"unknown op {op!r}; known: "
                   + ", ".join(s["id"] for s in SERVICE_SKILLS), "UNKNOWN_OP")


async def _run_command(text: str, meta: dict, session: Session, caller: ops.Caller,
                       room: Optional[str], background_tasks: BackgroundTasks,
                       ctx_out: Optional[str]) -> dict:
    cmd, _, rest = text[1:].partition(" ")
    cmd, rest = cmd.lower().strip(), rest.strip()
    if cmd in ("help", "start", ""):
        return _reply(HELP_TEXT, {"help": True, "skills": [s["id"] for s in SERVICE_SKILLS]}, ctx_out)
    mapping: dict[str, tuple[str, Callable[[str], dict]]] = {
        "read": ("read", lambda r: {"since": int(r)} if r.isdigit() else {}),
        "room": ("room_info", lambda r: {}),
        "rooms": ("list_rooms", lambda r: {}),
        "inbox": ("check_inbox", lambda r: {}),
        "create": ("create_room", lambda r: {"description": r}),
        "files": ("list_files", lambda r: {}),
        "fetch": ("fetch_file", lambda r: {"file_id": r}),
    }
    if cmd not in mapping:
        raise RpcError(INVALID_PARAMS, f"unknown command /{cmd}; try /help", "UNKNOWN_OP")
    op, build = mapping[cmd]
    return await _run_op(op, build(rest), meta, session, caller, room, background_tasks, ctx_out)
