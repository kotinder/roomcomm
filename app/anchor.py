"""External timestamp anchoring for the tamper-evident log.

The README has always been honest about the ceiling of the hash chain: the
arbiter and the platform share one process and one trust domain, so the chain
catches "someone edited the database behind the app" but not "someone owns the
whole server". Whoever owns the server can rewrite the rows *and* re-sign the
chain over them, and a later `verify` will happily answer CLEAN.

The fix the README named but never built: publish the head of the chain
somewhere the operator does not control, and do it on a schedule. After that,
rewriting history requires also rewriting a value that a third party already
saw at a known time — which is a different and much harder problem.

How this works:

1. Each room gets a **digest** over its whole visible state: every message
   (id, author, timestamp, hash of the text, any signature) plus the head of
   its arbiter revision chain. Deliberately computed from fields the public
   API already returns, so anybody holding a room UUID can recompute it
   without trusting us — see `GET /api/rooms/{uuid}/anchor`.
2. All room digests become leaves of a **Merkle tree**; the root covers the
   entire server state in 32 bytes.
3. The root is **published externally** with a timestamp, and stored here with
   whatever receipt the external service gave back.
4. Anyone can later ask for an **inclusion proof**: their room's digest, the
   sibling hashes up to a published root, and the receipt for that root. If
   the room's history had been altered after that publication, the recomputed
   digest would not reproduce the root.

What this still does not do: make the *content* true. A perfectly anchored
room can be full of confident nonsense. Anchoring is about "these are the same
bytes as before", nothing more.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
from typing import Optional

from sqlmodel import Session, select

from .models import Anchor, Claim, ClaimRevision, Message, Room, utcnow
from . import pcis, tsa

log = logging.getLogger("roomcomm.anchor")

#: Bumped if the digest definition ever changes — an old proof must never be
#: silently checked against a new rule.
DIGEST_VERSION = "rc-anchor-1"


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def message_line(m: Message) -> str:
    """One message, as the canonical string that feeds the room digest.

    Only fields the REST API already exposes, so a third party can rebuild
    this from `GET /api/rooms/{uuid}/messages` alone. The text is hashed
    rather than included: the digest must be reproducible by someone quoting
    the message, without newline-escaping arguments getting in the way.
    """
    return "|".join([
        str(m.id),
        m.agent_id or "",
        pcis.iso_canonical(m.timestamp),
        _sha256_hex((m.text or "").encode("utf-8")),
        m.pubkey_hex or "",
        m.signature_hex or "",
        m.memory_root or "",
    ])


def room_digest(session: Session, room_uuid: str) -> str:
    """Digest over a room's full visible state: messages + revision chain head."""
    messages = session.exec(
        select(Message).where(Message.room_uuid == room_uuid).order_by(Message.id.asc())
    ).all()
    chain_head = session.exec(
        select(ClaimRevision.row_hash)
        .join(Claim, ClaimRevision.claim_id == Claim.id)
        .where(Claim.room_uuid == room_uuid)
        .order_by(ClaimRevision.id.desc())
        .limit(1)
    ).first()

    payload = "\n".join([
        DIGEST_VERSION,
        room_uuid,
        str(len(messages)),
        *[message_line(m) for m in messages],
        f"chain_head={chain_head or ''}",
    ])
    return _sha256_hex(payload.encode("utf-8"))


def collect_leaves(session: Session) -> list[tuple[str, str]]:
    """(room_uuid, digest) for every room, ordered by UUID.

    Expired rooms are included on purpose: their history still exists and is
    exactly the kind of thing worth being unable to edit after the fact.
    """
    uuids = sorted(r for r in session.exec(select(Room.uuid)).all())
    return [(u, room_digest(session, u)) for u in uuids]


def leaf_hash(room_uuid: str, digest: str) -> str:
    # Domain-separated from internal nodes so a leaf can never be passed off
    # as a subtree (the classic second-preimage trick on Merkle trees).
    return _sha256_hex(f"leaf:{room_uuid}:{digest}".encode("utf-8"))


def _node_hash(left: str, right: str) -> str:
    return _sha256_hex(f"node:{left}:{right}".encode("utf-8"))


def merkle_root(leaves: list[tuple[str, str]]) -> str:
    """Root over the leaf list. An empty server anchors a constant, so the
    schedule still produces a receipt on a quiet day."""
    level = [leaf_hash(u, d) for u, d in leaves]
    if not level:
        return _sha256_hex(b"empty:" + DIGEST_VERSION.encode("utf-8"))
    while len(level) > 1:
        nxt = []
        for i in range(0, len(level), 2):
            left = level[i]
            # Odd node out is carried up unchanged rather than duplicated —
            # duplicating is the CVE-2012-2459 shape.
            right = level[i + 1] if i + 1 < len(level) else None
            nxt.append(_node_hash(left, right) if right else left)
        level = nxt
    return level[0]


def inclusion_proof(leaves: list[tuple[str, str]], room_uuid: str) -> Optional[list[dict]]:
    """Sibling path from a room's leaf to the root.

    Each step is {"side": "left"|"right", "hash": ...}: `side` says where the
    sibling sits, so a verifier can recompute without re-deriving the layout.
    Returns None if the room is not in this leaf set.
    """
    index = next((i for i, (u, _) in enumerate(leaves) if u == room_uuid), None)
    if index is None:
        return None

    level = [leaf_hash(u, d) for u, d in leaves]
    path: list[dict] = []
    while len(level) > 1:
        nxt = []
        for i in range(0, len(level), 2):
            left = level[i]
            right = level[i + 1] if i + 1 < len(level) else None
            if right is None:
                # Carried up alone: no sibling to record at this level.
                if i == index:
                    index = len(nxt)
                nxt.append(left)
                continue
            if i == index:
                path.append({"side": "right", "hash": right})
                index = len(nxt)
            elif i + 1 == index:
                path.append({"side": "left", "hash": left})
                index = len(nxt)
            nxt.append(_node_hash(left, right))
        level = nxt
    return path


def verify_proof(room_uuid: str, digest: str, path: list[dict], root: str) -> bool:
    """Recompute a root from a leaf and its sibling path. Pure function —
    the point is that a third party can run exactly this, offline, against a
    root we published somewhere we do not control."""
    current = leaf_hash(room_uuid, digest)
    for step in path:
        sibling = step["hash"]
        if step.get("side") == "left":
            current = _node_hash(sibling, current)
        else:
            current = _node_hash(current, sibling)
    return current == root


def build(session: Session) -> dict:
    """Compute the current anchor without storing or publishing it."""
    leaves = collect_leaves(session)
    root = merkle_root(leaves)
    return {
        "root": root,
        "leaf_count": len(leaves),
        "digest_version": DIGEST_VERSION,
        "leaves": leaves,
    }


def pack_leaves(leaves: list[tuple[str, str]]) -> bytes:
    return gzip.compress(
        json.dumps(leaves, separators=(",", ":")).encode("utf-8"), compresslevel=6
    )


def load_leaves(row: Anchor) -> Optional[list[tuple[str, str]]]:
    """Leaf set a stored anchor was built from, or None for older rows that
    predate storing them (their root and receipt are still meaningful; only
    the served proof is unavailable)."""
    if not row.leaves_gz:
        return None
    try:
        return [tuple(x) for x in json.loads(gzip.decompress(row.leaves_gz).decode("utf-8"))]
    except Exception:  # noqa: BLE001 — a corrupt blob must not take the endpoint down
        log.exception("anchor %s: stored leaves are unreadable", row.id)
        return None


def stamp(session: Session, row: Anchor) -> bool:
    """Ask a public timestamp authority to sign this anchor's root.

    This is the part of anchoring that does not rely on anyone trusting us:
    the TSA signs the time with its own key. Returns True if a token was
    stored. Failure is not fatal — the root stays, honestly unstamped.
    """
    got = tsa.timestamp(row.root)
    if got is None:
        log.warning("anchor %s: no TSA would stamp the root", row.id)
        return False
    url, token = got
    row.tsa_url = url
    row.tsa_token = token
    row.tsa_time = _token_time(token)
    session.add(row)
    session.commit()
    session.refresh(row)
    log.info("anchor %s timestamped by %s at %s", row.id, url, row.tsa_time)
    return True


def _token_time(token: bytes) -> Optional[str]:
    """Best-effort read of the signed time out of the token, for display only.

    The token itself is the evidence; this is a convenience so `/api/anchors`
    can show a date without every caller running openssl. A GeneralizedTime in
    a TSTInfo looks like 20260916134534Z, so find that shape rather than
    walking the whole ASN.1 structure.
    """
    import re
    # 0x18 = GeneralizedTime tag, 0x0f = 15 bytes ("YYYYMMDDHHMMSSZ").
    m = re.search(rb"\x18\x0f(\d{14}Z)", token)
    if not m:
        return None
    raw = m.group(1).decode("ascii")
    return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}T{raw[8:10]}:{raw[10:12]}:{raw[12:14]}Z"


def create(session: Session, *, receipt: Optional[str] = None,
           published_via: Optional[str] = None) -> Anchor:
    """Compute, store and return an anchor row.

    Stored even when publication fails: a root recorded locally is still a
    commitment we can publish late, and the gap is visible in `GET /api/anchors`
    rather than hidden.
    """
    built = build(session)
    row = Anchor(
        created_at=utcnow(),
        root=built["root"],
        leaf_count=built["leaf_count"],
        digest_version=built["digest_version"],
        receipt=receipt,
        published_via=published_via,
        leaves_gz=pack_leaves(built["leaves"]),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    log.info("anchor %s built over %d rooms (receipt=%s)",
             row.root[:16], row.leaf_count, receipt or "none")
    return row


def signed_statement(row: Anchor) -> dict:
    """The exact object published externally, plus the arbiter's signature over
    it. The signature is a convenience, not the security story — the security
    story is that the statement lands somewhere we cannot edit."""
    statement = {
        "service": "roomcomm",
        "digest_version": row.digest_version,
        "anchor_id": row.id,
        "root": row.root,
        "leaf_count": row.leaf_count,
        "created_at": row.created_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    canonical = json.dumps(statement, sort_keys=True, separators=(",", ":"))
    return {
        **statement,
        "arbiter_pubkey": pcis.arbiter_pubkey_hex(),
        "arbiter_signature": pcis.arbiter_sign_hex(canonical.encode("utf-8")),
    }


def format_for_publication(row: Anchor) -> str:
    """Human-readable form for the external channel. Deliberately compact:
    the root is the payload, everything else is context for whoever reads it
    months later."""
    st = signed_statement(row)
    return (
        f"roomcomm anchor #{st['anchor_id']}\n"
        f"root: {st['root']}\n"
        f"rooms: {st['leaf_count']}\n"
        f"at: {st['created_at']}\n"
        f"alg: {st['digest_version']}\n"
        f"arbiter: {st['arbiter_pubkey'][:16]}…\n"
        f"sig: {st['arbiter_signature'][:32]}…\n"
        f"verify: https://roomcomm.xyz/api/rooms/<uuid>/anchor"
    )
