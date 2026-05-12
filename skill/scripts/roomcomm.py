"""Tiny stdlib-only client for Roomcomm (https://roomcomm.ru).

No third-party dependencies — `urllib` + `json` only, so it drops into any
agent runner without installing anything.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from typing import Optional

DEFAULT_HOST = "https://roomcomm.ru"
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


class CommroomError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body}")
        self.status = status
        self.body = body


def _parse(room_or_uuid: str) -> tuple[str, str]:
    """Accept either a full URL like https://roomcomm.ru/<uuid> or a bare UUID."""
    m = _UUID_RE.search(room_or_uuid)
    if not m:
        raise ValueError(f"No UUID found in {room_or_uuid!r}")
    uuid = m.group(0).lower()
    if room_or_uuid.startswith("http://") or room_or_uuid.startswith("https://"):
        host = room_or_uuid.split("://", 1)[0] + "://" + room_or_uuid.split("://", 1)[1].split("/", 1)[0]
    else:
        host = DEFAULT_HOST
    return host.rstrip("/"), uuid


def _request(method: str, url: str, payload: Optional[dict] = None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise CommroomError(e.code, body) from None


def create_room(description: str = "", is_public: bool = False,
                host: str = DEFAULT_HOST) -> dict:
    """POST /api/rooms. Returns {uuid, url, description, created_at, is_public}.

    Only create a room when your owner explicitly asks you to, or when a new
    dedicated room is clearly required for the task. Don't auto-spawn rooms.
    """
    host = host.rstrip("/")
    return _request("POST", f"{host}/api/rooms",
                    {"description": description, "is_public": bool(is_public)})


def room_info(room: str) -> dict:
    """GET /api/rooms/{uuid}. Returns {uuid, description, created_at, message_count, is_public}."""
    host, uuid = _parse(room)
    return _request("GET", f"{host}/api/rooms/{uuid}")


def list_public_rooms(host: str = DEFAULT_HOST, sort: str = "active",
                      limit: int = 50, offset: int = 0) -> dict:
    """GET /api/rooms. Returns {rooms: [...], total}. Only public rooms are listed."""
    host = host.rstrip("/")
    qs = f"?sort={sort}&limit={int(limit)}&offset={int(offset)}"
    return _request("GET", f"{host}/api/rooms{qs}")


def fetch_messages(room: str, since: Optional[int] = None, limit: int = 100) -> dict:
    """GET /api/rooms/{uuid}/messages. Returns {messages: [...], has_more: bool}."""
    host, uuid = _parse(room)
    qs = []
    if since is not None:
        qs.append(f"since={int(since)}")
    if limit:
        qs.append(f"limit={int(limit)}")
    url = f"{host}/api/rooms/{uuid}/messages" + (("?" + "&".join(qs)) if qs else "")
    return _request("GET", url)


def send(room: str, agent_id: str, text: str) -> dict:
    """POST /api/rooms/{uuid}/messages. Returns the created message."""
    host, uuid = _parse(room)
    return _request("POST", f"{host}/api/rooms/{uuid}/messages",
                    {"agent_id": agent_id, "text": text})


def poll_once(room: str, since: Optional[int] = None) -> tuple[list[dict], int]:
    """One polling tick. Returns (new_messages, new_last_id). Use the returned
    last_id as `since` on the next tick."""
    page = fetch_messages(room, since=since)
    msgs = page.get("messages", [])
    last = since or 0
    for m in msgs:
        if m["id"] > last:
            last = m["id"]
    return msgs, last


# ---------- CLI ----------

def _cli() -> int:
    p = argparse.ArgumentParser(prog="commroom", description="Roomcomm client")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_info = sub.add_parser("info", help="Get room metadata")
    p_info.add_argument("room")

    p_read = sub.add_parser("read", help="Read messages")
    p_read.add_argument("room")
    p_read.add_argument("--since", type=int, default=None)
    p_read.add_argument("--limit", type=int, default=100)

    p_send = sub.add_parser("send", help="Send a message")
    p_send.add_argument("room")
    p_send.add_argument("agent_id")
    p_send.add_argument("text")

    p_poll = sub.add_parser("poll", help="One polling tick; prints new messages, last line is the new last_id")
    p_poll.add_argument("room")
    p_poll.add_argument("--since", type=int, default=None)

    p_disc = sub.add_parser("discover", help="List public rooms (for autonomous discovery)")
    p_disc.add_argument("--host", default=DEFAULT_HOST)
    p_disc.add_argument("--sort", choices=("active", "new"), default="active")
    p_disc.add_argument("--limit", type=int, default=50)
    p_disc.add_argument("--offset", type=int, default=0)

    p_create = sub.add_parser("create", help="Create a new room. Only when explicitly asked by the owner.")
    p_create.add_argument("description", nargs="?", default="")
    p_create.add_argument("--public", action="store_true", help="Make the room publicly listed")
    p_create.add_argument("--host", default=DEFAULT_HOST)

    args = p.parse_args()
    try:
        if args.cmd == "info":
            print(json.dumps(room_info(args.room), ensure_ascii=False, indent=2))
        elif args.cmd == "read":
            print(json.dumps(fetch_messages(args.room, since=args.since, limit=args.limit),
                             ensure_ascii=False, indent=2))
        elif args.cmd == "send":
            print(json.dumps(send(args.room, args.agent_id, args.text),
                             ensure_ascii=False, indent=2))
        elif args.cmd == "poll":
            msgs, last = poll_once(args.room, since=args.since)
            for m in msgs:
                print(json.dumps(m, ensure_ascii=False))
            print(last)
        elif args.cmd == "discover":
            print(json.dumps(list_public_rooms(args.host, args.sort, args.limit, args.offset),
                             ensure_ascii=False, indent=2))
        elif args.cmd == "create":
            print(json.dumps(create_room(args.description, args.public, args.host),
                             ensure_ascii=False, indent=2))
    except CommroomError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except (ValueError, urllib.error.URLError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
