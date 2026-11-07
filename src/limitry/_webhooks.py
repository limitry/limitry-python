"""Verifying a webhook delivery: `client.webhooks.verify(body, headers, secret)`.

Deliveries are signed Svix-compatibly (packages/jobs/src/webhooks.ts):
HMAC-SHA256 over `<webhook-id>.<webhook-timestamp>.<body>` with the
endpoint's secret (`whsec_<base64 key>`), sent as `webhook-signature:
v1,<base64>` (several, space-separated, while a secret rotates). The same
check as the TypeScript SDK's; standard library only.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from typing import Any, Dict, Mapping, Optional, TypedDict, Union


class WebhookEvent(TypedDict):
    """The event a delivery carries."""

    id: str
    """The event's id (`evt_…`), the same in every delivery of it: dedupe on it."""
    type: str
    """What happened, such as `member.joined`."""
    createdAt: str
    data: Dict[str, Any]


class WebhookVerificationError(Exception):
    """A delivery that failed verification: not from us, altered, or too old."""


def _header(headers: Mapping[str, Any], name: str) -> Optional[str]:
    for key, value in headers.items():
        if key.lower() == name:
            return value[0] if isinstance(value, (list, tuple)) else str(value)
    return None


def verify_webhook(
    body: Union[str, bytes],
    headers: Mapping[str, Any],
    secret: str,
    *,
    tolerance_seconds: int = 300,
    now: Optional[float] = None,
) -> WebhookEvent:
    """Checks a delivery and returns its event.

    `body` is the request body exactly as received (the raw text or bytes,
    before any JSON parsing); `headers` the request's headers; `secret` the
    endpoint's signing secret. Raises WebhookVerificationError when the
    signature doesn't match or the delivery is older than `tolerance_seconds`.
    """
    delivery_id = _header(headers, "webhook-id")
    timestamp = _header(headers, "webhook-timestamp")
    signatures = _header(headers, "webhook-signature")
    if not delivery_id or not timestamp or not signatures:
        raise WebhookVerificationError(
            "Missing webhook-id, webhook-timestamp or webhook-signature header"
        )
    try:
        sent_at = int(timestamp)
    except ValueError:
        raise WebhookVerificationError("The delivery's timestamp is invalid") from None
    if abs(int(now if now is not None else time.time()) - sent_at) > tolerance_seconds:
        raise WebhookVerificationError("The delivery is too old (or its timestamp is invalid)")
    text = body.decode("utf-8") if isinstance(body, bytes) else body
    key = base64.b64decode(secret[6:] if secret.startswith("whsec_") else secret)
    expected = hmac.new(
        key, f"{delivery_id}.{timestamp}.{text}".encode("utf-8"), hashlib.sha256
    ).digest()
    for part in signatures.split(" "):
        version, _, value = part.partition(",")
        if version != "v1" or not value:
            continue
        try:
            if hmac.compare_digest(base64.b64decode(value), expected):
                break
        except (binascii.Error, ValueError):
            continue
    else:
        raise WebhookVerificationError("The signature doesn't match")
    try:
        return json.loads(text)
    except ValueError:
        raise WebhookVerificationError("The body isn't JSON") from None


class Webhooks:
    """`client.webhooks`: checks deliveries your endpoint receives (calls nothing)."""

    def verify(
        self,
        body: Union[str, bytes],
        headers: Mapping[str, Any],
        secret: str,
        *,
        tolerance_seconds: int = 300,
    ) -> WebhookEvent:
        """Checks a delivery and returns its event (see verify_webhook)."""
        return verify_webhook(body, headers, secret, tolerance_seconds=tolerance_seconds)
