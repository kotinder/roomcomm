"""RFC 3161 trusted timestamps — no account, no dependencies.

The anchor needs its root fixed in time by somebody who is not us. A public
channel or a git repo makes the root *visible*, which is useful, but both are
still surfaces the operator controls: posts can be deleted, history can be
force-pushed. A timestamp authority is different in kind — it signs "this hash
was shown to me at this instant" with its own key, and no amount of access to
our server produces that signature or moves that time.

So this is the load-bearing half of anchoring, and the cheapest: a 69-byte
request to a free public TSA, no registration, no secret to store.

Verification is standard tooling, nothing of ours required:

    curl -o token.tsr https://roomcomm.xyz/api/anchors/1/tsa
    printf '%s' "<root from /api/anchors>" > root.txt
    openssl ts -verify -data root.txt -in token.tsr -CAfile <tsa-ca.pem>
    openssl ts -reply -in token.tsr -text        # shows the signed time

What a timestamp does NOT do: say anything about whether the conversation it
covers is true, or stop us deleting the anchor row. It proves that whatever we
published under that root has not changed since the TSA saw it.

The DER encoder below is deliberately hand-rolled. A TimeStampReq is four
fields; pulling in an ASN.1 dependency to build 69 bytes, in a service whose
whole point is that you should not have to trust it, is a worse trade.
"""
from __future__ import annotations

import hashlib
import logging
import os
from typing import Optional

import httpx

log = logging.getLogger("roomcomm.tsa")

#: Free, public, no-registration timestamp authorities, tried in order.
#: Several because any one of them can be down, and a missed daily anchor is a
#: gap in the record.
DEFAULT_AUTHORITIES = (
    "http://timestamp.digicert.com",
    "http://timestamp.sectigo.com",
    "http://tsa.mesign.com",
)

# sha256 OID (2.16.840.1.101.3.4.2.1) and ASN.1 NULL, pre-encoded.
_SHA256_OID = bytes.fromhex("0609608648016503040201")
_NULL = bytes.fromhex("0500")


def _der_len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    body = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(body)]) + body


def _tlv(tag: int, body: bytes) -> bytes:
    return bytes([tag]) + _der_len(len(body)) + body


def build_request(digest: bytes, nonce: Optional[bytes] = None) -> bytes:
    """A DER-encoded RFC 3161 TimeStampReq over a sha256 digest.

    The nonce ties this reply to this request: a replayed old token carries the
    wrong nonce and is spotted on verification.
    """
    if len(digest) != 32:
        raise ValueError("sha256 digest must be 32 bytes")
    nonce = nonce or os.urandom(8)
    # DER integers are signed — a leading high bit needs a zero byte, or the
    # value comes back negative and the TSA rejects the request.
    nonce_int = b"\x00" + nonce if nonce[0] & 0x80 else nonce

    algorithm = _tlv(0x30, _SHA256_OID + _NULL)
    imprint = _tlv(0x30, algorithm + _tlv(0x04, digest))
    version = _tlv(0x02, b"\x01")
    cert_req = _tlv(0x01, b"\xff")  # ask for the TSA cert inside the token
    return _tlv(0x30, version + imprint + _tlv(0x02, nonce_int) + cert_req)


def granted(token: bytes) -> bool:
    """Cheap sanity check on a reply: DER SEQUENCE and a plausible size.

    Full validation means checking the TSA's signature chain, which is what
    `openssl ts -verify` is for. We refuse to store something obviously broken
    but we do not pretend to have verified it here.
    """
    return bool(token) and token[:1] == b"\x30" and len(token) > 256


def timestamp(
    payload: str,
    authorities: Optional[tuple[str, ...]] = None,
    timeout: float = 25.0,
) -> Optional[tuple[str, bytes]]:
    """Get (authority_url, token_der) for a string, or None if all TSAs fail.

    Returning None rather than raising: a missed timestamp must leave the
    anchor stored and honestly marked unstamped, not abort the run.

    The authority list is read at call time, not bound as a default argument,
    so it can be swapped by configuration or by a test.
    """
    authorities = authorities or DEFAULT_AUTHORITIES
    digest = hashlib.sha256(payload.encode("utf-8")).digest()
    request = build_request(digest)
    for url in authorities:
        try:
            with httpx.Client(timeout=timeout) as client:
                r = client.post(
                    url,
                    content=request,
                    headers={"Content-Type": "application/timestamp-query"},
                )
            if r.status_code >= 400:
                log.warning("TSA %s answered %s", url, r.status_code)
                continue
            token = r.content
            if not granted(token):
                log.warning("TSA %s returned an unusable reply (%d bytes)", url, len(token))
                continue
            log.info("TSA %s timestamped %s… (%d bytes)", url, payload[:16], len(token))
            return url, token
        except Exception as e:  # noqa: BLE001 — any TSA failure moves to the next
            log.warning("TSA %s unreachable: %r", url, e)
    return None
