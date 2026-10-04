"""One-shot: point @RoomComm_bot's webhook at the server.

Usage (needs the same env the server runs with):
    TG_BOT_TOKEN=... TG_WEBHOOK_SECRET=... python scripts/tg_set_webhook.py
    TG_BOT_TOKEN=... python scripts/tg_set_webhook.py --delete   # switch off

The secret must match the server's TG_WEBHOOK_SECRET — Telegram echoes it in
X-Telegram-Bot-Api-Secret-Token and the webhook rejects everything else.
Only 'message' updates are subscribed; the bot keeps working as the outbound
notification sink regardless.
"""
import os
import sys

import httpx

URL = os.environ.get("TG_WEBHOOK_URL", "https://roomcomm.xyz/tg/webhook")
TOKEN = os.environ.get("TG_BOT_TOKEN", "")
SECRET = os.environ.get("TG_WEBHOOK_SECRET", "")

if not TOKEN:
    sys.exit("TG_BOT_TOKEN is not set")

api = f"https://api.telegram.org/bot{TOKEN}"

if "--delete" in sys.argv:
    r = httpx.post(f"{api}/deleteWebhook", timeout=15)
else:
    if not SECRET:
        sys.exit("TG_WEBHOOK_SECRET is not set (must match the server env)")
    r = httpx.post(f"{api}/setWebhook", timeout=15, json={
        "url": URL,
        "secret_token": SECRET,
        "allowed_updates": ["message"],
        "drop_pending_updates": True,
    })
print(r.status_code, r.text)
r = httpx.get(f"{api}/getWebhookInfo", timeout=15)
print(r.text)
