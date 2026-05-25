"""Telegram notification sink for platform events.

Configured via env: TG_BOT_TOKEN and TG_CHAT_ID. If either is missing,
every send_* call is a no-op — the server runs unchanged for anyone who
doesn't opt in.

Two transports are exposed because FastAPI exception handlers run sync and
background tasks run async; both share the same HTTP body shape.
"""
from __future__ import annotations

import html
import logging
import os
from typing import Optional

import httpx

log = logging.getLogger("roomcomm.notify")

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()

_API_URL = (
    f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    if TG_BOT_TOKEN
    else ""
)


def is_configured() -> bool:
    return bool(TG_BOT_TOKEN and TG_CHAT_ID)


def _payload(text: str) -> dict:
    return {
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }


async def send(text: str) -> None:
    if not is_configured():
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(_API_URL, json=_payload(text))
            if r.status_code >= 400:
                log.warning("telegram notify failed: %s %s", r.status_code, r.text[:200])
    except Exception as e:
        log.warning("telegram notify exception: %r", e)


def send_sync(text: str) -> None:
    if not is_configured():
        return
    try:
        with httpx.Client(timeout=10) as client:
            r = client.post(_API_URL, json=_payload(text))
            if r.status_code >= 400:
                log.warning("telegram notify failed: %s %s", r.status_code, r.text[:200])
    except Exception as e:
        log.warning("telegram notify exception: %r", e)


def format_room_created(
    *,
    room_url: str,
    uuid: str,
    description: str,
    is_public: bool,
    protocol_mode: str,
) -> str:
    visibility = "🌐 public" if is_public else "🔒 private"
    if protocol_mode == "premium":
        ledger = "✅ premium (LLM ledger on every message)"
    else:
        ledger = "⚪ standard (LLM ledger on demand)"
    desc = (description or "").strip()
    desc_line = f"\n<b>Description:</b> {html.escape(desc)}" if desc else ""
    return (
        f"🆕 <b>New roomcomm</b>\n"
        f'<a href="{html.escape(room_url)}">{html.escape(room_url)}</a>\n'
        f"<b>UUID:</b> <code>{html.escape(uuid)}</code>\n"
        f"<b>Visibility:</b> {visibility}\n"
        f"<b>Protocol:</b> {ledger}"
        f"{desc_line}"
    )


def format_error(
    *,
    where: str,
    exc: BaseException,
    request_path: Optional[str] = None,
) -> str:
    extra = (
        f"\n<b>Path:</b> <code>{html.escape(request_path)}</code>"
        if request_path
        else ""
    )
    return (
        f"⚠️ <b>Roomcomm internal error</b>\n"
        f"<b>Where:</b> {html.escape(where)}{extra}\n"
        f"<b>Error:</b> <code>{html.escape(repr(exc))[:600]}</code>"
    )
