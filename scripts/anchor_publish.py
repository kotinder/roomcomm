#!/usr/bin/env python3
"""Compute an anchor and publish its root outside this server.

Run on a schedule (daily is plenty). Each run takes a Merkle root over every
room's state, stores it, and posts it to an external channel that the server
operator cannot silently rewrite. From then on, altering any room's history
means also altering a value a third party already saw at a known time.

    python -m scripts.anchor_publish                 # compute, store, publish
    python -m scripts.anchor_publish --dry-run       # compute and print only
    python -m scripts.anchor_publish --target stdout # store, print, don't post

Every run asks a public RFC 3161 timestamp authority to sign the root. That
step needs no account, no channel and no secret, and it is the part an
operator cannot forge: the authority signs the time with its own key. The
token is served at /api/anchors/{id}/tsa for anyone to check with openssl.

Targets (the timestamp happens either way):

  tsa       Default. Timestamp only — nothing is posted anywhere.
  telegram  Also post the root to TG_ANCHOR_CHAT_ID. Makes the root easy to
            watch; it does not make it harder to forge.
  stdout    Also print the statement, for a cron job that pipes it elsewhere.

The anchor row is written even when publication fails, and shows up in
GET /api/anchors with receipt=null. An unpublished root is still a local
commitment worth keeping, and a visible gap is better than a quiet one.

Exit codes: 0 anchored, 1 computed but not anchored anywhere, 2 failed to compute.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from sqlmodel import Session  # noqa: E402

from app import anchor  # noqa: E402
from app.database import engine, init_db  # noqa: E402
from app.models import Anchor  # noqa: E402


def publish_telegram(text: str) -> str:
    """Post to Telegram, return a receipt. Raises on any failure."""
    token = os.environ.get("TG_BOT_TOKEN", "").strip()
    # Deliberately NO fallback to TG_CHAT_ID. That is the owner's private
    # notification chat, and a root nobody else can read proves nothing to
    # anybody — it would let the API report published: true on a claim no third
    # party can check, which is exactly the hollow guarantee this feature
    # exists to avoid. The anchor channel has to be chosen on purpose, and it
    # should be public.
    chat_id = os.environ.get("TG_ANCHOR_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise RuntimeError(
            "TG_BOT_TOKEN and TG_ANCHOR_CHAT_ID must both be set. Point "
            "TG_ANCHOR_CHAT_ID at a PUBLIC channel: an anchor read only by the "
            "operator is not evidence for anyone else."
        )
    with httpx.Client(timeout=20) as client:
        r = client.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
            },
        )
    if r.status_code >= 400:
        raise RuntimeError(f"telegram rejected the anchor: {r.status_code} {r.text[:200]}")
    body = r.json()
    result = body.get("result", {})
    message_id = result.get("message_id")
    chat = result.get("chat", {}) or {}
    username = chat.get("username")
    if username and message_id:
        return f"https://t.me/{username}/{message_id}"
    return f"tg:{chat_id}:{message_id}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=["tsa", "telegram", "stdout"], default="tsa",
                        help="tsa: get a third-party RFC 3161 timestamp (default, "
                             "needs no configuration). telegram: also post the root "
                             "to TG_ANCHOR_CHAT_ID. stdout: print it.")
    parser.add_argument("--dry-run", action="store_true",
                        help="compute and print, store nothing, publish nothing")
    args = parser.parse_args()

    init_db()

    try:
        with Session(engine) as session:
            built = anchor.build(session)
    except Exception as e:  # noqa: BLE001 — a scheduled job reports, it doesn't traceback
        print(f"anchor: failed to compute: {e}", file=sys.stderr)
        return 2

    if args.dry_run:
        print(f"root={built['root']} rooms={built['leaf_count']} "
              f"alg={built['digest_version']} (dry run, nothing stored)")
        return 0

    with Session(engine) as session:
        row = anchor.create(session)
        anchor_id = row.id
        # Always try for a trusted timestamp, whatever else we do with the
        # root. This is the half that does not rely on anyone trusting us:
        # the authority signs the time with its own key.
        stamped = anchor.stamp(session, row)
        text = anchor.format_for_publication(row)
        tsa_url, tsa_time = row.tsa_url, row.tsa_time

    if stamped:
        print(f"anchor #{anchor_id} timestamped by {tsa_url} at {tsa_time}")
    else:
        print(f"anchor #{anchor_id}: stored, but NO timestamp authority "
              f"would sign it — the root is not anchored yet", file=sys.stderr)

    if args.target == "stdout":
        print(text)
        return 0 if stamped else 1

    if args.target == "tsa":
        return 0 if stamped else 1

    try:
        receipt = publish_telegram(text)
    except Exception as e:  # noqa: BLE001
        # The root is already committed locally; say plainly that it is not yet
        # anchored anywhere external, and let the caller's monitoring see it.
        print(f"anchor #{anchor_id}: computed and stored, NOT published: {e}",
              file=sys.stderr)
        return 1

    with Session(engine) as session:
        row = session.get(Anchor, anchor_id)
        row.receipt = receipt
        row.published_via = "telegram"
        session.add(row)
        session.commit()

    print(f"anchor #{anchor_id} published: {receipt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
