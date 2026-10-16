"""The SDK's runtime: credentials, requests, errors.

Hand-written (packages/sdk-python/runtime/); the build copies it into the
product's package beside the generated methods (``_resources.py``) and types
(``_types.py``). Every method a resource has calls ``_call`` with its
operation's shape: method, path, which inputs go in the query string, and
how each Python argument is named on the wire.
"""

from __future__ import annotations

import asyncio
import os
import random
import time
import uuid
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Mapping, Optional, TypedDict, TypeVar
from urllib.parse import quote

import httpx

from ._meta import API_KEY_ENV, DEFAULT_API, PRODUCT_NAME, VERSION
from ._webhooks import Webhooks


class _NotGiven:
    """An argument left out — distinct from ``None``, which the API may mean."""

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "NOT_GIVEN"


NOT_GIVEN: Any = _NotGiven()
_C = TypeVar("_C", bound="Client")
_A = TypeVar("_A", bound="AsyncClient")
NotGiven = _NotGiven


class RequestOptions(TypedDict, total=False):
    """Per-request options: an idempotency key, more headers.

    Every write sends an ``Idempotency-Key`` anyway (one per call, reused on
    the client's own retries); pass yours to make a retry YOU make — a rerun
    job, a redelivered webhook — count once too.
    """

    idempotency_key: str
    headers: Mapping[str, str]


class ApiError(Exception):
    """A failed request: the API's error code and message, its HTTP status."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        request_id: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.request_id = request_id

    def __repr__(self) -> str:
        return f"ApiError(status={self.status}, code={self.code!r}, message={self.message!r})"


class _OperationShape(TypedDict):
    method: str
    path: str
    query: list
    body: bool
    names: Dict[str, str]


class Operation(_OperationShape, total=False):
    raw_body: str
    """Takes raw bytes as the body (the route's media type)."""
    raw_response: bool
    """Answers with raw bytes: the method returns them."""


# Safe retries, the rule every client follows (the TypeScript SDK's
# retry.ts): a write gets one Idempotency-Key per call, and each automatic
# retry of that call sends the same key, so the API runs it once. Retried:
# a network error or timeout, 408, 429, 500, 502, 503, 504, and 409
# ``idempotency_in_progress``; waits Retry-After when given (handing back
# one longer than _MAX_WAIT), else a jittered backoff capped at 8 seconds.
_WRITES = {"POST", "PUT", "PATCH", "DELETE"}
_RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
_MAX_WAIT = 20.0
DEFAULT_MAX_RETRIES = 2


def _backoff(attempt: int) -> float:
    return min(0.5 * 2**attempt, 8.0) * (0.75 + random.random() * 0.25)


def _retryable(response: httpx.Response) -> bool:
    if response.status_code in _RETRY_STATUSES:
        return True
    if response.status_code != 409:
        return False
    try:
        return (response.json().get("error") or {}).get("code") == "idempotency_in_progress"
    except ValueError:
        return False


def _wait(response: httpx.Response, attempt: int) -> Optional[float]:
    """Seconds to wait before the retry, or None to hand the answer back."""
    value = response.headers.get("retry-after")
    if not value:
        return _backoff(attempt)
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = parsedate_to_datetime(value).timestamp() - time.time()
        except (TypeError, ValueError):
            return _backoff(attempt)
    seconds = max(0.0, seconds)
    return None if seconds > _MAX_WAIT else seconds


def _error(response: httpx.Response) -> ApiError:
    try:
        envelope = response.json().get("error") or {}
    except ValueError:
        envelope = {}
    return ApiError(
        response.status_code,
        envelope.get("code") or "http_error",
        envelope.get("message") or f"HTTP {response.status_code}",
        envelope.get("requestId"),
    )


class _Base:
    def __init__(
        self,
        token: Optional[str] = None,
        *,
        workspace: Optional[str] = None,
        environment: Optional[str] = None,
        api: Optional[str] = None,
        timeout: float = 60.0,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        token = token or os.environ.get(API_KEY_ENV)
        if not token:
            raise ValueError(
                f"{PRODUCT_NAME}: no API key. Pass token= or set {API_KEY_ENV}."
            )
        self._base_url = (api or DEFAULT_API).rstrip("/") + "/v1"
        self.webhooks = Webhooks()
        """Checks deliveries your endpoint receives: `client.webhooks.verify(...)`."""
        self._timeout = timeout
        self._max_retries = max_retries
        self._headers: Dict[str, str] = {
            "authorization": f"Bearer {token}",
            "user-agent": f"{PRODUCT_NAME.lower()}-python/{VERSION}",
        }
        if workspace:
            self._headers["x-workspace"] = workspace
        if environment:
            self._headers["x-environment"] = environment

    def _request(
        self,
        op: Operation,
        path_params: Mapping[str, Any],
        inputs: Mapping[str, Any],
        options: Optional[RequestOptions],
        content: Optional[bytes] = None,
        content_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        path = op["path"]
        for name, value in path_params.items():
            path = path.replace("{" + name + "}", quote(str(value), safe=""))
        query: Dict[str, Any] = {}
        body: Dict[str, Any] = {}
        for name, value in inputs.items():
            if value is NOT_GIVEN:
                continue
            wire = op["names"].get(name, name)
            (query if wire in op["query"] else body)[wire] = value
        headers = dict(self._headers)
        if options:
            headers.update(options.get("headers") or {})
            key = options.get("idempotency_key")
            if key:
                headers["idempotency-key"] = key
        if op["method"].upper() in _WRITES and "idempotency-key" not in headers:
            headers["idempotency-key"] = str(uuid.uuid4())
        request: Dict[str, Any] = {
            "method": op["method"].upper(),
            "url": self._base_url + path,
            "params": query or None,
            "headers": headers,
        }
        if op["body"]:
            request["json"] = body
        raw_body = op.get("raw_body")
        if raw_body:
            request["content"] = content
            headers["content-type"] = content_type or raw_body
        return request

    @staticmethod
    def _answer(response: httpx.Response, op: Operation) -> Any:
        if response.status_code >= 400:
            raise _error(response)
        if op.get("raw_response"):
            return response.content
        if response.status_code == 204 or not response.content:
            return None
        return response.json()


class Client(_Base):
    """The synchronous client."""

    def __init__(
        self,
        token: Optional[str] = None,
        *,
        workspace: Optional[str] = None,
        environment: Optional[str] = None,
        api: Optional[str] = None,
        timeout: float = 60.0,
        max_retries: int = DEFAULT_MAX_RETRIES,
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        super().__init__(
            token,
            workspace=workspace,
            environment=environment,
            api=api,
            timeout=timeout,
            max_retries=max_retries,
        )
        self._http = http_client or httpx.Client(timeout=timeout)

    def _call(
        self,
        op: Operation,
        path_params: Mapping[str, Any],
        inputs: Mapping[str, Any],
        options: Optional[RequestOptions] = None,
        content: Optional[bytes] = None,
        content_type: Optional[str] = None,
    ) -> Any:
        request = self._request(op, path_params, inputs, options, content, content_type)
        attempt = 0
        while True:
            last = attempt >= self._max_retries
            try:
                response = self._http.request(**request)
            except httpx.TransportError:
                if last:
                    raise
                time.sleep(_backoff(attempt))
                attempt += 1
                continue
            delay = None if last or not _retryable(response) else _wait(response, attempt)
            if delay is None:
                return self._answer(response, op)
            time.sleep(delay)
            attempt += 1

    def close(self) -> None:
        self._http.close()

    def __enter__(self: _C) -> _C:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class AsyncClient(_Base):
    """The asynchronous client (``await``)."""

    def __init__(
        self,
        token: Optional[str] = None,
        *,
        workspace: Optional[str] = None,
        environment: Optional[str] = None,
        api: Optional[str] = None,
        timeout: float = 60.0,
        max_retries: int = DEFAULT_MAX_RETRIES,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        super().__init__(
            token,
            workspace=workspace,
            environment=environment,
            api=api,
            timeout=timeout,
            max_retries=max_retries,
        )
        self._http = http_client or httpx.AsyncClient(timeout=timeout)

    async def _call(
        self,
        op: Operation,
        path_params: Mapping[str, Any],
        inputs: Mapping[str, Any],
        options: Optional[RequestOptions] = None,
        content: Optional[bytes] = None,
        content_type: Optional[str] = None,
    ) -> Any:
        request = self._request(op, path_params, inputs, options, content, content_type)
        attempt = 0
        while True:
            last = attempt >= self._max_retries
            try:
                response = await self._http.request(**request)
            except httpx.TransportError:
                if last:
                    raise
                await asyncio.sleep(_backoff(attempt))
                attempt += 1
                continue
            delay = None if last or not _retryable(response) else _wait(response, attempt)
            if delay is None:
                return self._answer(response, op)
            await asyncio.sleep(delay)
            attempt += 1

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self: _A) -> _A:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()
