"""The errors the SDK raises: one tree, the same as the TypeScript SDK's.

    <Product>Error                 every error the SDK raises (``ProductError`` here)
    ├── APIConnectionError         the API couldn't be reached (``__cause__``: httpx's error)
    │   └── APITimeoutError        an attempt timed out, after the retries
    ├── APIError                   the API answered with an error
    │   ├── BadRequestError        400
    │   ├── AuthenticationError    401
    │   ├── PlanLimitError         402
    │   ├── PermissionDeniedError  403
    │   ├── NotFoundError          404
    │   ├── ConflictError          409
    │   ├── UnprocessableEntityError 422
    │   ├── RateLimitError         429
    │   └── InternalServerError    500 and above
    └── WebhookVerificationError   a delivery that failed verification

The base takes the product's name (``_error.py``, generated from brand.json);
a product's helpers subclass it for their own errors. httpx's errors never
escape the client.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Type

import httpx

from ._error import ProductError


class APIConnectionError(ProductError):
    """The API couldn't be reached: a network failure, after the retries.
    httpx's error is the ``__cause__``."""

    def __init__(self, message: str = "Connection error.", *, request: Optional[httpx.Request] = None) -> None:
        super().__init__(message)
        self.message = message
        self.request = request


class APITimeoutError(APIConnectionError):
    """An attempt took longer than its ``timeout``, after the retries."""

    def __init__(self, message: str = "Request timed out.", *, request: Optional[httpx.Request] = None) -> None:
        super().__init__(message, request=request)


class APIError(ProductError):
    """The API answered with an error: its HTTP status, the error's code and
    message, the request's id, the response's headers, and the error's
    ``details`` (each a ``{"path", "message"}``) and ``docs`` link."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        code: str,
        request_id: Optional[str] = None,
        headers: Optional[httpx.Headers] = None,
        details: Optional[List[Dict[str, Any]]] = None,
        docs: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code
        self.request_id = request_id
        self.headers = headers if headers is not None else httpx.Headers()
        """The response's headers: ``Retry-After``, the rate-limit headers."""
        self.details = details or []
        """What was wrong with each field: ``[{"path": "url", "message": "..."}]``."""
        self.docs = docs
        """A link to the error code's entry on the docs' Errors page."""

    def __str__(self) -> str:
        lines = [self.message]
        lines += [f"  {d.get('path') or '(body)'}: {d.get('message')}" for d in self.details if isinstance(d, dict)]
        return "\n".join(lines)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(status_code={self.status_code}, code={self.code!r}, message={self.message!r})"


class BadRequestError(APIError):
    """400: the request was malformed or a field was wrong (see ``details``)."""


class AuthenticationError(APIError):
    """401: the key or token is missing, wrong or revoked."""


class PlanLimitError(APIError):
    """402: the workspace's plan doesn't allow this, or a limit is used up."""


class PermissionDeniedError(APIError):
    """403: the credential may not do this, or its workspace is suspended.

    A suspended workspace's ``code`` is ``workspace_suspended``.
    """


class NotFoundError(APIError):
    """404: no such resource, or not one this credential can see."""


class ConflictError(APIError):
    """409: the request conflicts with the resource's state."""


class UnprocessableEntityError(APIError):
    """422: the request was well formed but can't be carried out."""


class RateLimitError(APIError):
    """429: too many requests; ``headers["retry-after"]`` says how long to wait."""


class InternalServerError(APIError):
    """500 and above: the API failed; the call was retried first."""


_BY_STATUS: Dict[int, Type[APIError]] = {
    400: BadRequestError,
    401: AuthenticationError,
    402: PlanLimitError,
    403: PermissionDeniedError,
    404: NotFoundError,
    409: ConflictError,
    422: UnprocessableEntityError,
    429: RateLimitError,
}


def error_for(response: httpx.Response) -> APIError:
    """The error a failed response raises, by its status."""
    try:
        envelope = response.json().get("error") or {}
    except (ValueError, AttributeError):
        envelope = {}
    if not isinstance(envelope, dict):
        envelope = {}
    status = response.status_code
    cls = _BY_STATUS.get(status) or (InternalServerError if status >= 500 else APIError)
    details = envelope.get("details")
    return cls(
        envelope.get("message") or f"HTTP {status}",
        status_code=status,
        code=envelope.get("code") or "http_error",
        request_id=envelope.get("requestId"),
        headers=response.headers,
        details=details if isinstance(details, list) else None,
        docs=envelope.get("docs"),
    )


__all__ = [
    "ProductError",
    "APIConnectionError",
    "APITimeoutError",
    "APIError",
    "BadRequestError",
    "AuthenticationError",
    "PlanLimitError",
    "PermissionDeniedError",
    "NotFoundError",
    "ConflictError",
    "UnprocessableEntityError",
    "RateLimitError",
    "InternalServerError",
    "error_for",
]
