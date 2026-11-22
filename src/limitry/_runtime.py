"""The SDK's runtime: credentials, requests, retries, errors.

Hand-written (packages/python-sdk/runtime/); the build copies it into the
product's package beside the generated methods (``_resources.py``), the
response models (``types.py``) and the request types (``_params.py``).
Every method a resource has calls ``_call`` (a list ``_page``) with its
operation's shape: method, path, which inputs go in the query string, and
how each Python argument is named on the wire. A product's helpers call
them too, with the generated methods' keywords: ``self._call(op, {},
inputs, idempotency_key=..., extra_headers=..., timeout=...)``. Without
``cast_to`` the answer is the API's JSON as it came.
"""

from __future__ import annotations

import asyncio
import copy
import os
import random
import time
import uuid
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Mapping, Optional, TypedDict, TypeVar, Union
from urllib.parse import quote

import httpx

from ._exceptions import APIConnectionError, APITimeoutError, error_for
from ._meta import API_KEY_ENV, BASE_URL_ENV, DEFAULT_BASE_URL, PRODUCT_NAME, TOKEN_ENV, USER_AGENT
from ._models import construct_type
from ._pagination import AsyncPage, AsyncPaginator, SyncPage
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

Timeout = Union[float, httpx.Timeout, None]
"""Seconds each attempt may take (or an ``httpx.Timeout``; ``None``: no limit)."""


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
_MAX_WAIT = 60.0
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
    except (ValueError, AttributeError):
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


def _failure(error: Exception) -> Optional[Exception]:
    """httpx's error as the SDK's (None: not httpx's, so not ours to change)."""
    if isinstance(error, httpx.TimeoutException):
        return APITimeoutError()
    if isinstance(error, (httpx.HTTPError, httpx.InvalidURL)):
        return APIConnectionError(f"Connection error: {error}" if str(error) else "Connection error.")
    return None


# Timeouts, as in other Python API SDKs (owner, 2026-11-22: "pythonic
# and match similar sdks"): ``timeout`` limits each attempt (the client's,
# 60 s by default; a method's ``timeout=`` or ``with_options`` for some
# calls), and a timed-out attempt is retried with the same key. To limit a
# whole call, retries included, cancel its task: ``asyncio.timeout()``
# around an AsyncClient call stops it at once, mid-request or mid-wait.
DEFAULT_TIMEOUT = 60.0


class _Base:
    def __init__(
        self,
        token: Optional[str] = None,
        *,
        api_key: Optional[str] = None,
        workspace: Optional[str] = None,
        environment: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: Timeout = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        default_headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        # api_key for a Workspace API key, token for a personal access
        # token (owner, 2026-11-06); token still takes a key, as it always
        # has. Without either: <NAME>_API_KEY, then the CLI's <NAME>_TOKEN.
        if api_key and token:
            raise ValueError(f"{PRODUCT_NAME}: pass api_key or token, not both.")
        token = api_key or token or os.environ.get(API_KEY_ENV) or os.environ.get(TOKEN_ENV)
        if not token:
            raise ValueError(f"{PRODUCT_NAME}: no API key. Pass api_key= or set {API_KEY_ENV}.")
        # The API's origin, without /v1 (the TypeScript SDK's baseURL).
        self._origin = (base_url or os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL).rstrip("/")
        self.webhooks = Webhooks()
        """Checks deliveries your endpoint receives: `client.webhooks.verify(...)`."""
        self._timeout = timeout
        self._max_retries = max_retries
        self._headers = httpx.Headers(
            {
                "authorization": f"Bearer {token}",
                "user-agent": USER_AGENT,
            }
        )
        if workspace:
            self._headers["x-workspace"] = workspace
        if environment:
            self._headers["x-environment"] = environment
        self._headers.update(default_headers or {})

    @property
    def base_url(self) -> str:
        """The API's origin, which the client adds ``/v1`` to."""
        return self._origin

    def with_options(
        self: Any,
        *,
        timeout: Any = NOT_GIVEN,
        max_retries: Any = NOT_GIVEN,
        default_headers: Optional[Mapping[str, str]] = None,
    ) -> Any:
        """The same client with other defaults for the calls made through it:
        ``client.with_options(timeout=5).workspace.get()``. ``default_headers``
        are added to the client's. It shares this client's connections, so
        closing either closes both."""
        clone = copy.copy(self)
        clone._headers = httpx.Headers(self._headers)
        clone._headers.update(default_headers or {})
        if timeout is not NOT_GIVEN:
            clone._timeout = timeout
        if max_retries is not NOT_GIVEN:
            clone._max_retries = max_retries
        bind = getattr(clone, "_bind", None)
        if bind:
            bind()  # the generated resources, pointed at the copy
        return clone

    def _request(
        self,
        op: Operation,
        path_params: Mapping[str, Any],
        inputs: Mapping[str, Any],
        *,
        idempotency_key: Optional[str],
        extra_headers: Optional[Mapping[str, str]],
        content: Optional[bytes],
        content_type: Optional[str],
        timeout: Any,
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
        headers = httpx.Headers(self._headers)
        headers.update(extra_headers or {})
        if idempotency_key:
            headers["idempotency-key"] = idempotency_key
        if op["method"].upper() in _WRITES and "idempotency-key" not in headers:
            headers["idempotency-key"] = str(uuid.uuid4())
        raw_body = op.get("raw_body")
        if raw_body:
            headers["content-type"] = content_type or raw_body
        request: Dict[str, Any] = {
            "method": op["method"].upper(),
            "url": self._origin + "/v1" + path,
            "params": query or None,
            "headers": headers,
            "timeout": self._timeout if timeout is NOT_GIVEN else timeout,
        }
        if op["body"]:
            request["json"] = body
        if raw_body:
            request["content"] = content
        return request

    @staticmethod
    def _answer(response: httpx.Response, op: Operation, cast_to: Any) -> Any:
        if response.status_code >= 400:
            raise error_for(response)
        if op.get("raw_response"):
            return response.content
        if response.status_code == 204 or not response.content:
            return None
        data = response.json()
        return data if cast_to is NOT_GIVEN else construct_type(data, cast_to)


class Client(_Base):
    """The synchronous client."""

    def __init__(
        self,
        token: Optional[str] = None,
        *,
        api_key: Optional[str] = None,
        workspace: Optional[str] = None,
        environment: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: Timeout = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        default_headers: Optional[Mapping[str, str]] = None,
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        super().__init__(
            token,
            api_key=api_key,
            workspace=workspace,
            environment=environment,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            default_headers=default_headers,
        )
        self._http = http_client or httpx.Client(timeout=timeout)

    def _call(
        self,
        op: Operation,
        path_params: Mapping[str, Any],
        inputs: Mapping[str, Any],
        *,
        content: Optional[bytes] = None,
        content_type: Optional[str] = None,
        cast_to: Any = NOT_GIVEN,
        idempotency_key: Optional[str] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
        timeout: Any = NOT_GIVEN,
    ) -> Any:
        request = self._request(
            op,
            path_params,
            inputs,
            idempotency_key=idempotency_key,
            extra_headers=extra_headers,
            content=content,
            content_type=content_type,
            timeout=timeout,
        )
        attempt = 0
        while True:
            last = attempt >= self._max_retries
            try:
                response = self._http.request(**request)
            except Exception as error:
                failure = _failure(error)
                if failure is None:
                    raise
                if last or not isinstance(error, httpx.TransportError):
                    raise failure from error
                time.sleep(_backoff(attempt))
                attempt += 1
                continue
            delay = None if last or not _retryable(response) else _wait(response, attempt)
            if delay is None:
                return self._answer(response, op, cast_to)
            time.sleep(delay)
            attempt += 1

    def _page(
        self,
        op: Operation,
        path_params: Mapping[str, Any],
        inputs: Mapping[str, Any],
        *,
        item: Any,
        extra_headers: Optional[Mapping[str, str]] = None,
        timeout: Any = NOT_GIVEN,
    ) -> SyncPage[Any]:
        def fetch(cursor: Optional[str]) -> SyncPage[Any]:
            data = self._call(
                op,
                path_params,
                {**inputs, "cursor": cursor} if cursor else inputs,
                extra_headers=extra_headers,
                timeout=timeout,
            )
            return SyncPage(data or {}, item, fetch)

        return fetch(None)

    def close(self) -> None:
        """Closes the client's connections."""
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
        api_key: Optional[str] = None,
        workspace: Optional[str] = None,
        environment: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: Timeout = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        default_headers: Optional[Mapping[str, str]] = None,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        super().__init__(
            token,
            api_key=api_key,
            workspace=workspace,
            environment=environment,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            default_headers=default_headers,
        )
        self._http = http_client or httpx.AsyncClient(timeout=timeout)

    async def _call(
        self,
        op: Operation,
        path_params: Mapping[str, Any],
        inputs: Mapping[str, Any],
        *,
        content: Optional[bytes] = None,
        content_type: Optional[str] = None,
        cast_to: Any = NOT_GIVEN,
        idempotency_key: Optional[str] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
        timeout: Any = NOT_GIVEN,
    ) -> Any:
        request = self._request(
            op,
            path_params,
            inputs,
            idempotency_key=idempotency_key,
            extra_headers=extra_headers,
            content=content,
            content_type=content_type,
            timeout=timeout,
        )
        attempt = 0
        while True:
            last = attempt >= self._max_retries
            try:
                response = await self._http.request(**request)
            except Exception as error:
                failure = _failure(error)
                if failure is None:
                    raise
                if last or not isinstance(error, httpx.TransportError):
                    raise failure from error
                await asyncio.sleep(_backoff(attempt))
                attempt += 1
                continue
            delay = None if last or not _retryable(response) else _wait(response, attempt)
            if delay is None:
                return self._answer(response, op, cast_to)
            await asyncio.sleep(delay)
            attempt += 1

    def _page(
        self,
        op: Operation,
        path_params: Mapping[str, Any],
        inputs: Mapping[str, Any],
        *,
        item: Any,
        extra_headers: Optional[Mapping[str, str]] = None,
        timeout: Any = NOT_GIVEN,
    ) -> AsyncPaginator[Any]:
        async def fetch(cursor: Optional[str]) -> AsyncPage[Any]:
            data = await self._call(
                op,
                path_params,
                {**inputs, "cursor": cursor} if cursor else inputs,
                extra_headers=extra_headers,
                timeout=timeout,
            )
            return AsyncPage(data or {}, item, fetch)

        return AsyncPaginator(fetch)

    async def close(self) -> None:
        """Closes the client's connections."""
        await self._http.aclose()

    async def aclose(self) -> None:
        """The same as ``close()``, by httpx's name."""
        await self.close()

    async def __aenter__(self: _A) -> _A:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()
