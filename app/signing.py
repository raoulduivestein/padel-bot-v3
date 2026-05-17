from __future__ import annotations

import base64
import hashlib
import hmac
from hashlib import sha256
from urllib.parse import urlparse

from app.config import SignatureMode


def canonical_message(
    *,
    mode: SignatureMode,
    method: str,
    url: str,
    timestamp: int,
    body: str,
    device_id: str = "",
    content_type: str = "application/json",
) -> bytes:
    parsed = urlparse(url)
    path = parsed.path
    query = f"?{parsed.query}" if parsed.query else ""
    path_with_query = f"{path}{query}"
    method_upper = method.upper()

    if mode == "davidlloyd_v1":
        parts = [method_upper, path_with_query]
        if body:
            parts.extend([content_type, hashlib.md5(body.encode("utf-8")).hexdigest()])
        parts.extend([device_id, str(timestamp)])
        message = "\n".join(parts)
    elif mode == "timestamp_body":
        message = f"{timestamp}{body}"
    elif mode == "timestamp_method_path_body":
        message = f"{timestamp}{method_upper}{path_with_query}{body}"
    else:
        message = f"{method_upper}{path_with_query}{timestamp}{body}"
    return message.encode("utf-8")


def build_signature(
    *,
    mode: SignatureMode,
    public_key_b64: str,
    hmac_key: bytes,
    method: str,
    url: str,
    timestamp: int,
    body: str,
    device_id: str,
    content_type: str = "application/json",
) -> str:
    message = canonical_message(
        mode=mode,
        method=method,
        url=url,
        timestamp=timestamp,
        body=body,
        device_id=device_id,
        content_type=content_type,
    )
    digest = hmac.new(hmac_key, message, sha256).digest()
    signature = base64.b64encode(digest).decode("ascii")
    return f"v1 {public_key_b64[:12]}:{signature}"
