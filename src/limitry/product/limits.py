# Limitry's helpers (docs/product/roadmap.md slice 6), in Python: the two
# shapes a product's code takes around a costly call, as `with` blocks.
#
#     limitry = Limitry()  # reads LIMITRY_API_KEY
#
#     # Known cost: check, then run the block only if allowed.
#     with limitry.guard(subject={"kind": "apikey", "id": key.id}, action="api-request"):
#         response = handle(request)
#
#     # Cost known afterwards (an AI reply's cents), or "at most N at
#     # once": hold an estimate, do the work, commit what it used —
#     # released on an error.
#     with limitry.hold(subject={"kind": "user", "id": user.id}, action="ai-call", estimate=5) as held:
#         answer = call_model(prompt)
#         held.cost(price_cents(answer.usage))
#
# A refusal raises `LimitExceeded` (with `retry_after_seconds`). When
# Limitry can't be reached (an error, a 5xx, or no answer within
# `timeout`) — or the workspace is past its plan's allowance (402) — each
# limit's fail mode decides, as in the TypeScript SDK: the last decision's
# fail mode for this subject and action is remembered, and `closed`
# refuses (reason `unavailable`) while `open`, the default, runs the
# block unmetered.
#
# Retries are safe: each guard or hold sends one Idempotency-Key, and a
# check that failed fast (a connection error, a 5xx) is sent once more
# with it, so it counts once. A hold whose check timed out may have been
# granted all the same: once the block is done, `hold` sends the check
# again with the same key (a replay, nothing counted twice) and releases
# the hold it finds. Commits and releases retry too.

from __future__ import annotations

import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager, contextmanager
from typing import (
    Any,
    AsyncIterator,
    Dict,
    Iterator,
    List,
    Literal,
    Optional,
    TypedDict,
)

import httpx

from .._runtime import ApiError


class Subject(TypedDict):
    kind: str
    id: str


FailMode = Literal["open", "closed"]
_UNREACHABLE: Any = object()
# No answer in time: unlike an error, the check may have been decided.
_TIMED_OUT: Any = object()
# A settle (or a replay to find a hold): per attempt, and how many.
_SETTLE_TIMEOUT = 5.0
_SETTLE_ATTEMPTS = 3
_MEMORY = 10_000


class LimitExceeded(Exception):
    """Not allowed: a limit is used up — or Limitry is unreachable and a limit fails closed."""

    def __init__(self, decision: Optional[Dict[str, Any]]) -> None:
        reason = ((decision or {}).get("reason") or {}).get("code") or "unavailable"
        limit = ((decision or {}).get("reason") or {}).get("limit")
        if reason == "unavailable":
            message = "Limitry could not be reached and a limit denies when unreachable"
        elif reason == "too_many_reservations":
            message = "Too many open reservations for this subject"
        else:
            message = f"Limit {limit or ''} exceeded".replace("  ", " ")
        super().__init__(message)
        self.reason: str = reason
        """`limit_exceeded`, `too_many_reservations` or `unavailable`."""
        self.limit: Optional[str] = limit
        """The limit that refused; None when unavailable or too many holds."""
        self.retry_after_seconds: Optional[int] = (decision or {}).get("retryAfterSeconds")
        self.decision: Optional[Dict[str, Any]] = decision


class Held:
    """A hold in progress: report what the work used, keep it for longer work."""

    def __init__(self, estimate: float, extend: Any = None) -> None:
        self.used = estimate
        self._extend = extend

    def cost(self, units: float) -> None:
        """The cost actually used — committed when the block ends. Default: the estimate."""
        self.used = units

    def extend(self, ttl_seconds: Optional[int] = None) -> None:
        """Keep the hold for longer work: it expires `ttl_seconds` from now (default 300)."""
        if self._extend:
            self._extend(ttl_seconds)


class AsyncHeld(Held):
    """A hold in progress, in `async with limitry.hold(...)`."""

    async def extend(self, ttl_seconds: Optional[int] = None) -> None:  # type: ignore[override]
        """Keep the hold for longer work: it expires `ttl_seconds` from now (default 300)."""
        if self._extend:
            await self._extend(ttl_seconds)


def _check_body(
    subject: Subject,
    action: str,
    cost: float,
    shared: Optional[List[Subject]],
    reserve: bool = False,
    ttl_seconds: Optional[int] = None,
) -> Dict[str, Any]:
    body: Dict[str, Any] = {"subject": subject, "action": action, "cost": cost}
    if shared:
        body["shared"] = shared
    if reserve:
        body["mode"] = "reserve"
        if ttl_seconds is not None:
            body["ttlSeconds"] = ttl_seconds
    return body


def _raise_for(response: httpx.Response) -> None:
    """An extend that failed is the caller's to know (the hold may have expired)."""
    if response.status_code >= 400:
        try:
            error = response.json().get("error") or {}
        except ValueError:
            error = {}
        raise ApiError(
            response.status_code,
            error.get("code") or "http_error",
            error.get("message") or f"HTTP {response.status_code}",
            error.get("requestId"),
        )


def _answer(response: httpx.Response) -> Any:
    """The decision, or _UNREACHABLE; a 4xx other than 402 is the caller's mistake."""
    if response.status_code < 300:
        return response.json()
    if response.status_code == 402 or response.status_code >= 500:
        return _UNREACHABLE
    try:
        error = response.json().get("error") or {}
    except ValueError:
        error = {}
    raise ApiError(
        response.status_code,
        error.get("code") or "http_error",
        f"Limitry refused the check: {error.get('message') or response.status_code}",
        error.get("requestId"),
    )


class _Memory:
    """The last fail mode seen per subject + action (closed if any limit is)."""

    def __init__(self) -> None:
        self._seen: "OrderedDict[tuple, FailMode]" = OrderedDict()

    def mode(self, subject: Subject, action: str, forced: Optional[FailMode]) -> FailMode:
        return forced or self._seen.get((subject["kind"], subject["id"], action), "open")

    def remember(self, subject: Subject, action: str, decision: Dict[str, Any]) -> None:
        closed = any(r.get("failMode") == "closed" for r in decision.get("remaining") or [])
        self._seen[(subject["kind"], subject["id"], action)] = "closed" if closed else "open"
        if len(self._seen) > _MEMORY:
            self._seen.popitem(last=False)


class ProductMethods:
    """`limitry.guard(...)`, `limitry.hold(...)` and `limitry.standing(...)`."""

    _http: httpx.Client
    _base_url: str
    _headers: Dict[str, str]

    def _limits_memory(self) -> _Memory:
        memory = self.__dict__.get("_limitry_memory")
        if memory is None:
            memory = self.__dict__["_limitry_memory"] = _Memory()
        return memory

    def _check(self, body: Dict[str, Any], timeout: float, key: str) -> Any:
        headers = {**self._headers, "idempotency-key": key}
        for attempt in range(2):
            try:
                response = self._http.post(
                    self._base_url + "/checks", json=body, headers=headers, timeout=timeout
                )
            except httpx.TimeoutException:
                return _TIMED_OUT
            except httpx.HTTPError:
                if attempt == 0:
                    continue
                return _UNREACHABLE
            if response.status_code >= 500 and attempt == 0:
                continue
            return _answer(response)
        return _UNREACHABLE

    def _release_unheard(self, body: Dict[str, Any], key: str) -> None:
        # After a hold's check timed out: ask again with the same key (a
        # replay if Limitry decided it) and release any hold it granted.
        replay = self._check(body, _SETTLE_TIMEOUT, key)
        if isinstance(replay, dict) and replay.get("reservation"):
            self._settle(f"/reservations/{replay['reservation']['id']}/release", None)

    def _settle(self, path: str, body: Optional[Dict[str, Any]]) -> None:
        # Settling never fails the caller's work: it is retried (the same
        # settle answers the same), and a lost one expires by itself.
        for _ in range(_SETTLE_ATTEMPTS):
            try:
                response = self._http.post(
                    self._base_url + path, json=body, headers=self._headers, timeout=_SETTLE_TIMEOUT
                )
            except httpx.HTTPError:
                continue
            if response.status_code < 500:
                return

    @contextmanager
    def guard(
        self,
        *,
        subject: Subject,
        action: str,
        cost: float = 1,
        shared: Optional[List[Subject]] = None,
        timeout: float = 1.5,
        fail_mode: Optional[FailMode] = None,
    ) -> Iterator[None]:
        """Checks `cost`, and runs the block only if allowed (else raises `LimitExceeded`)."""
        memory = self._limits_memory()
        decision = self._check(
            _check_body(subject, action, cost, shared), timeout, str(uuid.uuid4())
        )
        if decision is _UNREACHABLE or decision is _TIMED_OUT:
            if memory.mode(subject, action, fail_mode) == "closed":
                raise LimitExceeded(None)
        else:
            if not fail_mode:
                memory.remember(subject, action, decision)
            if not decision.get("allowed"):
                raise LimitExceeded(decision)
        yield

    @contextmanager
    def hold(
        self,
        *,
        subject: Subject,
        action: str,
        estimate: float = 1,
        shared: Optional[List[Subject]] = None,
        ttl_seconds: Optional[int] = None,
        timeout: float = 1.5,
        fail_mode: Optional[FailMode] = None,
    ) -> Iterator[Held]:
        """Holds `estimate`, runs the block, then commits the cost it reported
        (`held.cost(n)`, default the estimate) — or releases the hold if the
        block raises. A concurrent limit's slot is held exactly as long as the
        block runs."""
        memory = self._limits_memory()
        body = _check_body(subject, action, estimate, shared, True, ttl_seconds)
        key = str(uuid.uuid4())
        decision = self._check(body, timeout, key)
        if decision is _UNREACHABLE or decision is _TIMED_OUT:
            timed_out = decision is _TIMED_OUT
            if memory.mode(subject, action, fail_mode) == "closed":
                if timed_out:
                    self._release_unheard(body, key)
                raise LimitExceeded(None)
            try:
                yield Held(estimate)
            finally:
                if timed_out:
                    self._release_unheard(body, key)
            return
        if not fail_mode:
            memory.remember(subject, action, decision)
        reservation = decision.get("reservation")
        if not decision.get("allowed") or not reservation:
            raise LimitExceeded(decision)
        path = f"/reservations/{reservation['id']}"

        def extend(ttl_seconds: Optional[int]) -> None:
            body = {"ttlSeconds": ttl_seconds} if ttl_seconds else {}
            _raise_for(
                self._http.post(
                    self._base_url + path + "/extend", json=body, headers=self._headers, timeout=timeout
                )
            )

        held = Held(estimate, extend)
        try:
            yield held
        except BaseException:
            self._settle(path + "/release", None)
            raise
        self._settle(path + "/commit", {"cost": held.used})

    def standing(self, *, subject: Subject, shared: Optional[List[Subject]] = None) -> Dict[str, Any]:
        """Where a subject stands on every limit on it: used, remaining,
        resetsAt (the answer keeps the API's camelCase keys) — for showing
        an end user. Not a check: nothing is counted."""
        params = {"kind": subject["kind"], "id": subject["id"]}
        if shared:
            params["shared"] = ",".join(f"{s['kind']}:{s['id']}" for s in shared)
        response = self._http.get(
            self._base_url + "/usage/standing", params=params, headers=self._headers
        )
        if response.status_code >= 400:
            error = (response.json() if response.content else {}).get("error") or {}
            raise ApiError(
                response.status_code,
                error.get("code") or "http_error",
                error.get("message") or f"HTTP {response.status_code}",
                error.get("requestId"),
            )
        return response.json()


class AsyncProductMethods:
    """The same, to `async with` / `await`."""

    _http: httpx.AsyncClient
    _base_url: str
    _headers: Dict[str, str]

    def _limits_memory(self) -> _Memory:
        memory = self.__dict__.get("_limitry_memory")
        if memory is None:
            memory = self.__dict__["_limitry_memory"] = _Memory()
        return memory

    async def _check(self, body: Dict[str, Any], timeout: float, key: str) -> Any:
        headers = {**self._headers, "idempotency-key": key}
        for attempt in range(2):
            try:
                response = await self._http.post(
                    self._base_url + "/checks", json=body, headers=headers, timeout=timeout
                )
            except httpx.TimeoutException:
                return _TIMED_OUT
            except httpx.HTTPError:
                if attempt == 0:
                    continue
                return _UNREACHABLE
            if response.status_code >= 500 and attempt == 0:
                continue
            return _answer(response)
        return _UNREACHABLE

    async def _release_unheard(self, body: Dict[str, Any], key: str) -> None:
        # After a hold's check timed out: ask again with the same key (a
        # replay if Limitry decided it) and release any hold it granted.
        replay = await self._check(body, _SETTLE_TIMEOUT, key)
        if isinstance(replay, dict) and replay.get("reservation"):
            await self._settle(f"/reservations/{replay['reservation']['id']}/release", None)

    async def _settle(self, path: str, body: Optional[Dict[str, Any]]) -> None:
        for _ in range(_SETTLE_ATTEMPTS):
            try:
                response = await self._http.post(
                    self._base_url + path, json=body, headers=self._headers, timeout=_SETTLE_TIMEOUT
                )
            except httpx.HTTPError:
                continue
            if response.status_code < 500:
                return

    @asynccontextmanager
    async def guard(
        self,
        *,
        subject: Subject,
        action: str,
        cost: float = 1,
        shared: Optional[List[Subject]] = None,
        timeout: float = 1.5,
        fail_mode: Optional[FailMode] = None,
    ) -> AsyncIterator[None]:
        """Checks `cost`, and runs the block only if allowed (else raises `LimitExceeded`)."""
        memory = self._limits_memory()
        decision = await self._check(
            _check_body(subject, action, cost, shared), timeout, str(uuid.uuid4())
        )
        if decision is _UNREACHABLE or decision is _TIMED_OUT:
            if memory.mode(subject, action, fail_mode) == "closed":
                raise LimitExceeded(None)
        else:
            if not fail_mode:
                memory.remember(subject, action, decision)
            if not decision.get("allowed"):
                raise LimitExceeded(decision)
        yield

    @asynccontextmanager
    async def hold(
        self,
        *,
        subject: Subject,
        action: str,
        estimate: float = 1,
        shared: Optional[List[Subject]] = None,
        ttl_seconds: Optional[int] = None,
        timeout: float = 1.5,
        fail_mode: Optional[FailMode] = None,
    ) -> AsyncIterator[AsyncHeld]:
        """Holds `estimate`, runs the block, then commits what it used — or releases on an error."""
        memory = self._limits_memory()
        body = _check_body(subject, action, estimate, shared, True, ttl_seconds)
        key = str(uuid.uuid4())
        decision = await self._check(body, timeout, key)
        if decision is _UNREACHABLE or decision is _TIMED_OUT:
            timed_out = decision is _TIMED_OUT
            if memory.mode(subject, action, fail_mode) == "closed":
                if timed_out:
                    await self._release_unheard(body, key)
                raise LimitExceeded(None)
            try:
                yield AsyncHeld(estimate)
            finally:
                if timed_out:
                    await self._release_unheard(body, key)
            return
        if not fail_mode:
            memory.remember(subject, action, decision)
        reservation = decision.get("reservation")
        if not decision.get("allowed") or not reservation:
            raise LimitExceeded(decision)
        path = f"/reservations/{reservation['id']}"

        async def extend(ttl_seconds: Optional[int]) -> None:
            body = {"ttlSeconds": ttl_seconds} if ttl_seconds else {}
            _raise_for(
                await self._http.post(
                    self._base_url + path + "/extend", json=body, headers=self._headers, timeout=timeout
                )
            )

        held = AsyncHeld(estimate, extend)
        try:
            yield held
        except BaseException:
            await self._settle(path + "/release", None)
            raise
        await self._settle(path + "/commit", {"cost": held.used})

    async def standing(
        self, *, subject: Subject, shared: Optional[List[Subject]] = None
    ) -> Dict[str, Any]:
        """Where a subject stands on every limit on it. Not a check: nothing is counted."""
        params = {"kind": subject["kind"], "id": subject["id"]}
        if shared:
            params["shared"] = ",".join(f"{s['kind']}:{s['id']}" for s in shared)
        response = await self._http.get(
            self._base_url + "/usage/standing", params=params, headers=self._headers
        )
        if response.status_code >= 400:
            error = (response.json() if response.content else {}).get("error") or {}
            raise ApiError(
                response.status_code,
                error.get("code") or "http_error",
                error.get("message") or f"HTTP {response.status_code}",
                error.get("requestId"),
            )
        return response.json()
