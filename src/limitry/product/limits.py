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
#     # once": reserve what it will probably cost, do the work, commit
#     # what it used — released on an error.
#     with limitry.reserve(subject={"kind": "user", "id": user.id}, action="ai-call", cost=5) as reservation:
#         answer = call_model(prompt)
#         reservation.cost(price_cents(answer.usage))
#
# A refusal raises `LimitReached` (with `limit_id` and
# `retry_after_seconds`). When Limitry can't be reached — an error, a
# 5xx, no answer within `timeout`, the workspace past its plan's
# allowance (402) or the edge's flood guard (429) — each limit's fail
# mode decides, as in the TypeScript SDK: the last decision's fail mode
# for this subject and action is remembered, and `closed` raises
# `Unavailable` while `open`, the default, runs the block unmetered.
# One exception, as in the TypeScript SDK: an `open` concurrent limit is
# held to its amount in this process (design-fallback.md). `reserve`
# counts the blocks it has going per subject and action, answered or not,
# and remembers the smallest concurrent limit's `amount` from the last
# decision; while Limitry can't answer, a reserve that would pass it
# raises `Unavailable`. The count is this process's only: several servers
# may together run more, bounded where it was unbounded.
#
# Retries are safe, and follow the TypeScript SDK's rule exactly: each
# guard or reserve sends one Idempotency-Key, and a check is retried with
# it (a network error, 408, 429, 5xx; at most twice, the client's
# backoff) all within ONE time budget, `timeout` (1.5 s) — so it counts
# once and never waits longer than that. A reservation whose check timed
# out may have been made all the same: once the block is done, `reserve`
# sends the check again with the same key (a replay, nothing counted
# twice) and releases the reservation it finds. Commits and releases
# retry the same way within the same `timeout`.
#
# A subject being handed over between Limitry's servers (a split, a few
# seconds) answers `503 moving`, and a limiter with too many checks
# inside at once `503 busy`: Limitry was reached and charged nothing. The
# check, commit or release is sent again with the same key only while the
# wait still fits in `timeout` — the wait is the answer's Retry-After (at
# least a second) plus full jitter on a growing backoff, the TypeScript
# SDK's `retryWaitMs` — and then the fail mode applies. These helpers sit
# in front of a user, so they answer within `timeout` whatever Limitry
# says; and a refused check that comes back once, not for half a minute,
# doesn't keep an overloaded limiter full.

from __future__ import annotations

import asyncio
import random
import threading
import time
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

from .._runtime import (
    DEFAULT_MAX_RETRIES,
    ApiError,
    Operation,
    _backoff,
    _retryable,
    _wait,
)


class Subject(TypedDict):
    kind: str
    id: str


FailMode = Literal["open", "closed"]
_UNREACHABLE: Any = object()
# No answer in time: unlike an error, the check may have been decided.
_TIMED_OUT: Any = object()
# The backoff added to a `moving` or `busy` wait: full jitter under base × 2^n, capped (seconds).
_JITTER_BASE = 0.25
_JITTER_CAP = 4.0
_MEMORY = 10_000
# The largest cost a commit takes.
_MAX_COST = 1_000_000_000
_STANDING: Operation = {
    "method": "GET",
    "path": "/usage/standing",
    "query": ["subject", "shared"],
    "body": False,
    "names": {},
}


class LimitReached(Exception):
    """Refused: a limit is reached, or the subject holds too many open reservations."""

    def __init__(self, decision: Dict[str, Any]) -> None:
        reason = decision.get("reason") or {}
        # The API's own sentence (reason.message), one wording everywhere.
        super().__init__(reason.get("message") or "Refused")
        self.reason: str = reason.get("code") or "limit_reached"
        """`limit_reached` or `too_many_reservations`."""
        self.limit_id: Optional[str] = reason.get("limitId")
        """The limit that refused; None for too many open reservations."""
        self.retry_after_seconds: Optional[int] = decision.get("retryAfterSeconds")
        self.decision: Dict[str, Any] = decision


class Unavailable(Exception):
    """Limitry could not be reached (an error, a 5xx, a timeout, the plan's
    allowance used up, or the flood guard's 429) and a limit on this subject
    and action fails closed. With `open`, the block runs instead."""

    def __init__(self) -> None:
        super().__init__("Limitry could not be reached and a limit refuses when unreachable")


class Reservation:
    """A reservation in progress: report what the work used, keep it for longer work."""

    def __init__(self, reserved: float, extend: Any = None) -> None:
        self.used = reserved
        self._extend = extend

    def cost(self, units: float) -> None:
        """The cost actually used — committed when the block ends. Default: the reserved cost.

        A whole number from 0 to 1,000,000,000, as the API commits: anything
        else raises ValueError here, in your code, rather than a commit the
        API refuses after the work (whose usage would go uncounted)."""
        if (
            isinstance(units, bool)
            or not isinstance(units, (int, float))
            or not float(units).is_integer()
            or not 0 <= units <= _MAX_COST
        ):
            raise ValueError(
                f"reservation.cost({units!r}): the cost must be a whole number from 0 to {_MAX_COST}"
            )
        self.used = int(units)

    def extend(self, ttl_seconds: Optional[int] = None) -> None:
        """Keep the reservation for longer work: it expires `ttl_seconds` from now
        (default 300, 1 to 3600). Raises the API's error, such as for an expired
        reservation."""
        if self._extend:
            self._extend(ttl_seconds)


class AsyncReservation(Reservation):
    """A reservation in progress, in `async with limitry.reserve(...)`."""

    async def extend(self, ttl_seconds: Optional[int] = None) -> None:  # type: ignore[override]
        """Keep the reservation for longer work: it expires `ttl_seconds` from now (default 300)."""
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
    """An extend that failed is the caller's to know (the reservation may have expired)."""
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
    """The decision, or _UNREACHABLE (5xx, 402 past the plan, 429 the flood
    guard); any other 4xx is the caller's mistake."""
    if response.status_code < 300:
        return response.json()
    if response.status_code in (402, 429) or response.status_code >= 500:
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


def _retry_wait(retry_after: float, attempt: int) -> float:
    """The wait before asking again after a `moving` or `busy` answer, the
    `attempt`th in a row (0 first): Retry-After, at least a second (a 0, or
    a date past, must not spin), plus anywhere from 0 to
    min(_JITTER_CAP, _JITTER_BASE × 2^attempt)."""
    ceiling = min(_JITTER_CAP, _JITTER_BASE * 2**attempt)
    return max(1.0, retry_after) + random.random() * ceiling


def _moving(response: httpx.Response, attempt: int = 0) -> Optional[float]:
    """Seconds to wait before asking again, for a `503 moving` or `503
    busy` (nothing charged: retry with the same key); else None."""
    if response.status_code != 503:
        return None
    try:
        code = (response.json().get("error") or {}).get("code")
    except ValueError:
        return None
    if code not in ("moving", "busy"):
        return None
    try:
        retry_after = float(response.headers.get("retry-after") or 1)
    except ValueError:
        retry_after = 1.0
    return _retry_wait(retry_after, attempt)


def _standing_query(subject: Subject, shared: Optional[List[Subject]]) -> Dict[str, Any]:
    query: Dict[str, Any] = {"subject": f"{subject['kind']}:{subject['id']}"}
    if shared:
        query["shared"] = ",".join(f"{s['kind']}:{s['id']}" for s in shared)
    return query


class _Budget:
    """What is left of a time budget, called: the time since the start, or
    the waits slept, whichever is more (a test's fake sleep takes none)."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self.started = time.monotonic()
        self.slept = 0.0

    def __call__(self) -> float:
        return self.seconds - max(time.monotonic() - self.started, self.slept)


class _Memory:
    """The last fail mode seen per subject + action (closed if any limit is);
    the smallest concurrent limit's amount last seen, and the reserves this
    client has going, answered or not (design-fallback.md)."""

    def __init__(self) -> None:
        self._seen: "OrderedDict[tuple, FailMode]" = OrderedDict()
        self._amounts: "OrderedDict[tuple, int]" = OrderedDict()
        self._running: Dict[tuple, int] = {}
        self._lock = threading.Lock()

    def start(self, subject: Subject, action: str) -> int:
        """Counts a reserve as going; returns how many already were."""
        key = (subject["kind"], subject["id"], action)
        with self._lock:
            before = self._running.get(key, 0)
            self._running[key] = before + 1
        return before

    def finish(self, subject: Subject, action: str) -> None:
        key = (subject["kind"], subject["id"], action)
        with self._lock:
            left = self._running.get(key, 1) - 1
            if left > 0:
                self._running[key] = left
            else:
                self._running.pop(key, None)

    def held(self, subject: Subject, action: str, before: int) -> bool:
        """An open concurrent limit's amount already reached in this process."""
        amount = self._amounts.get((subject["kind"], subject["id"], action))
        return amount is not None and before >= amount

    def remember_amount(self, subject: Subject, action: str, decision: Dict[str, Any]) -> None:
        key = (subject["kind"], subject["id"], action)
        amounts = [
            r["amount"]
            for r in decision.get("limits") or []
            if r.get("window") == "concurrent" and isinstance(r.get("amount"), int)
        ]
        if amounts:
            self._amounts[key] = min(amounts)
            if len(self._amounts) > _MEMORY:
                self._amounts.popitem(last=False)
        else:
            self._amounts.pop(key, None)

    def mode(self, subject: Subject, action: str, forced: Optional[FailMode]) -> FailMode:
        return forced or self._seen.get((subject["kind"], subject["id"], action), "open")

    def remember(self, subject: Subject, action: str, decision: Dict[str, Any]) -> None:
        closed = any(r.get("failMode") == "closed" for r in decision.get("limits") or [])
        self._seen[(subject["kind"], subject["id"], action)] = "closed" if closed else "open"
        if len(self._seen) > _MEMORY:
            self._seen.popitem(last=False)


class ProductMethods:
    """`limitry.guard(...)`, `limitry.reserve(...)` and `limitry.standing(...)`."""

    _http: httpx.Client
    _base_url: str
    _headers: Dict[str, str]
    _call: Any

    def _limits_memory(self) -> _Memory:
        memory = self.__dict__.get("_limitry_memory")
        if memory is None:
            memory = self.__dict__["_limitry_memory"] = _Memory()
        return memory

    def _send(self, path: str, body: Any, key: str, budget: float) -> Any:
        """The answer — or _TIMED_OUT (the budget ran out) or _UNREACHABLE
        (a network error on the last try). The client's retry rule, inside
        one time budget: the TypeScript SDK's `check` and `settle`."""
        headers = {**self._headers, "idempotency-key": key}
        left = _Budget(budget)
        attempt = 0
        moving_tries = 0
        while True:
            remaining = left()
            if remaining <= 0:
                return _TIMED_OUT
            last = attempt >= getattr(self, "_max_retries", DEFAULT_MAX_RETRIES)
            try:
                response = self._http.post(
                    self._base_url + path, json=body, headers=headers, timeout=remaining
                )
            except httpx.TimeoutException:
                return _TIMED_OUT
            except httpx.HTTPError:
                if last:
                    return _UNREACHABLE
                delay: Optional[float] = _backoff(attempt)
            else:
                moving = _moving(response, moving_tries)
                if moving is not None:
                    moving_tries += 1
                    # Reached, nothing counted: asked again only while the wait fits.
                    if moving >= left():
                        return _UNREACHABLE
                    left.slept += moving
                    time.sleep(moving)
                    continue
                delay = None if last or not _retryable(response) else _wait(response, attempt)
                if delay is None:
                    return response
            if delay >= left():
                return _TIMED_OUT
            left.slept += delay
            time.sleep(delay)
            attempt += 1

    def _check(self, body: Dict[str, Any], timeout: float, key: str) -> Any:
        response = self._send("/checks", body, key, timeout)
        return response if response is _TIMED_OUT or response is _UNREACHABLE else _answer(response)

    def _release_unheard(self, body: Dict[str, Any], key: str, timeout: float) -> None:
        # After a reservation's check timed out: ask again with the same key
        # (a replay if Limitry decided it) and release any reservation it made.
        replay = self._check(body, timeout, key)
        if isinstance(replay, dict) and replay.get("reservation"):
            self._settle(f"/reservations/{replay['reservation']['id']}/release", None, timeout)

    def _settle(self, path: str, body: Optional[Dict[str, Any]], timeout: float) -> None:
        # Committing or releasing never fails the caller's work: it is
        # retried (the same commit answers the same) within `timeout`, as
        # it runs before the block's caller goes on; a lost one expires by
        # itself (an expired reservation is not charged).
        self._send(path, body, str(uuid.uuid4()), timeout)

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
        """Checks `cost`, and runs the block only if allowed (else raises `LimitReached`)."""
        memory = self._limits_memory()
        decision = self._check(
            _check_body(subject, action, cost, shared), timeout, str(uuid.uuid4())
        )
        if decision is _UNREACHABLE or decision is _TIMED_OUT:
            if memory.mode(subject, action, fail_mode) == "closed":
                raise Unavailable()
        else:
            if not fail_mode:
                memory.remember(subject, action, decision)
            if not decision.get("allowed"):
                raise LimitReached(decision)
        yield

    @contextmanager
    def reserve(
        self,
        *,
        subject: Subject,
        action: str,
        cost: float = 1,
        shared: Optional[List[Subject]] = None,
        ttl_seconds: Optional[int] = None,
        timeout: float = 1.5,
        fail_mode: Optional[FailMode] = None,
    ) -> Iterator[Reservation]:
        """Reserves `cost`, runs the block, then commits the cost it reported
        (`reservation.cost(n)`, default the reserved cost) — or releases the
        reservation if the block raises. A concurrent limit's slot is taken
        exactly as long as the block runs."""
        memory = self._limits_memory()
        before = memory.start(subject, action)
        try:
            with self._reserve(memory, before, subject, action, cost, shared, ttl_seconds, timeout, fail_mode) as r:
                yield r
        finally:
            memory.finish(subject, action)

    @contextmanager
    def _reserve(
        self,
        memory: _Memory,
        before: int,
        subject: Subject,
        action: str,
        cost: float,
        shared: Optional[List[Subject]],
        ttl_seconds: Optional[int],
        timeout: float,
        fail_mode: Optional[FailMode],
    ) -> Iterator[Reservation]:
        body = _check_body(subject, action, cost, shared, True, ttl_seconds)
        key = str(uuid.uuid4())
        decision = self._check(body, timeout, key)
        if decision is _UNREACHABLE or decision is _TIMED_OUT:
            timed_out = decision is _TIMED_OUT
            # Open, but never past a concurrent limit's amount here.
            if memory.mode(subject, action, fail_mode) == "closed" or memory.held(subject, action, before):
                if timed_out:
                    self._release_unheard(body, key, timeout)
                raise Unavailable()
            try:
                yield Reservation(cost)
            finally:
                if timed_out:
                    self._release_unheard(body, key, timeout)
            return
        memory.remember_amount(subject, action, decision)
        if not fail_mode:
            memory.remember(subject, action, decision)
        reservation = decision.get("reservation")
        if not decision.get("allowed") or not reservation:
            raise LimitReached(decision)
        path = f"/reservations/{reservation['id']}"

        def extend(ttl_seconds: Optional[int]) -> None:
            # Passed as given: the API refuses a bad one (0, past 3600).
            body = {} if ttl_seconds is None else {"ttlSeconds": ttl_seconds}
            _raise_for(
                self._http.post(
                    self._base_url + path + "/extend", json=body, headers=self._headers, timeout=timeout
                )
            )

        reserved = Reservation(cost, extend)
        try:
            yield reserved
        except BaseException:
            self._settle(path + "/release", None, timeout)
            raise
        self._settle(path + "/commit", {"cost": reserved.used}, timeout)

    def standing(self, *, subject: Subject, shared: Optional[List[Subject]] = None) -> Dict[str, Any]:
        """Where a subject stands on every limit on it: used, remaining,
        resetsAt (the answer keeps the API's camelCase keys) — for showing
        an end user. Not a check: nothing is counted."""
        # The client's own call: its retries, and its errors (any body).
        return self._call(_STANDING, {}, _standing_query(subject, shared))


class AsyncProductMethods:
    """The same, to `async with` / `await`."""

    _http: httpx.AsyncClient
    _base_url: str
    _headers: Dict[str, str]
    _call: Any

    def _limits_memory(self) -> _Memory:
        memory = self.__dict__.get("_limitry_memory")
        if memory is None:
            memory = self.__dict__["_limitry_memory"] = _Memory()
        return memory

    async def _send(self, path: str, body: Any, key: str, budget: float) -> Any:
        """The answer, _TIMED_OUT or _UNREACHABLE (see ProductMethods._send)."""
        headers = {**self._headers, "idempotency-key": key}
        left = _Budget(budget)
        attempt = 0
        moving_tries = 0
        while True:
            remaining = left()
            if remaining <= 0:
                return _TIMED_OUT
            last = attempt >= getattr(self, "_max_retries", DEFAULT_MAX_RETRIES)
            try:
                response = await self._http.post(
                    self._base_url + path, json=body, headers=headers, timeout=remaining
                )
            except httpx.TimeoutException:
                return _TIMED_OUT
            except httpx.HTTPError:
                if last:
                    return _UNREACHABLE
                delay: Optional[float] = _backoff(attempt)
            else:
                moving = _moving(response, moving_tries)
                if moving is not None:
                    moving_tries += 1
                    # Reached, nothing counted: asked again only while the wait fits.
                    if moving >= left():
                        return _UNREACHABLE
                    left.slept += moving
                    await asyncio.sleep(moving)
                    continue
                delay = None if last or not _retryable(response) else _wait(response, attempt)
                if delay is None:
                    return response
            if delay >= left():
                return _TIMED_OUT
            left.slept += delay
            await asyncio.sleep(delay)
            attempt += 1

    async def _check(self, body: Dict[str, Any], timeout: float, key: str) -> Any:
        response = await self._send("/checks", body, key, timeout)
        return response if response is _TIMED_OUT or response is _UNREACHABLE else _answer(response)

    async def _release_unheard(self, body: Dict[str, Any], key: str, timeout: float) -> None:
        # After a reservation's check timed out: ask again with the same key
        # (a replay if Limitry decided it) and release any reservation it made.
        replay = await self._check(body, timeout, key)
        if isinstance(replay, dict) and replay.get("reservation"):
            await self._settle(f"/reservations/{replay['reservation']['id']}/release", None, timeout)

    async def _settle(self, path: str, body: Optional[Dict[str, Any]], timeout: float) -> None:
        await self._send(path, body, str(uuid.uuid4()), timeout)

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
        """Checks `cost`, and runs the block only if allowed (else raises `LimitReached`)."""
        memory = self._limits_memory()
        decision = await self._check(
            _check_body(subject, action, cost, shared), timeout, str(uuid.uuid4())
        )
        if decision is _UNREACHABLE or decision is _TIMED_OUT:
            if memory.mode(subject, action, fail_mode) == "closed":
                raise Unavailable()
        else:
            if not fail_mode:
                memory.remember(subject, action, decision)
            if not decision.get("allowed"):
                raise LimitReached(decision)
        yield

    @asynccontextmanager
    async def reserve(
        self,
        *,
        subject: Subject,
        action: str,
        cost: float = 1,
        shared: Optional[List[Subject]] = None,
        ttl_seconds: Optional[int] = None,
        timeout: float = 1.5,
        fail_mode: Optional[FailMode] = None,
    ) -> AsyncIterator[AsyncReservation]:
        """Reserves `cost`, runs the block, then commits what it used — or releases on an error."""
        memory = self._limits_memory()
        before = memory.start(subject, action)
        try:
            async with self._reserve(memory, before, subject, action, cost, shared, ttl_seconds, timeout, fail_mode) as r:
                yield r
        finally:
            memory.finish(subject, action)

    @asynccontextmanager
    async def _reserve(
        self,
        memory: _Memory,
        before: int,
        subject: Subject,
        action: str,
        cost: float,
        shared: Optional[List[Subject]],
        ttl_seconds: Optional[int],
        timeout: float,
        fail_mode: Optional[FailMode],
    ) -> AsyncIterator[AsyncReservation]:
        body = _check_body(subject, action, cost, shared, True, ttl_seconds)
        key = str(uuid.uuid4())
        decision = await self._check(body, timeout, key)
        if decision is _UNREACHABLE or decision is _TIMED_OUT:
            timed_out = decision is _TIMED_OUT
            # Open, but never past a concurrent limit's amount here.
            if memory.mode(subject, action, fail_mode) == "closed" or memory.held(subject, action, before):
                if timed_out:
                    await self._release_unheard(body, key, timeout)
                raise Unavailable()
            try:
                yield AsyncReservation(cost)
            finally:
                if timed_out:
                    await self._release_unheard(body, key, timeout)
            return
        memory.remember_amount(subject, action, decision)
        if not fail_mode:
            memory.remember(subject, action, decision)
        reservation = decision.get("reservation")
        if not decision.get("allowed") or not reservation:
            raise LimitReached(decision)
        path = f"/reservations/{reservation['id']}"

        async def extend(ttl_seconds: Optional[int]) -> None:
            # Passed as given: the API refuses a bad one (0, past 3600).
            body = {} if ttl_seconds is None else {"ttlSeconds": ttl_seconds}
            _raise_for(
                await self._http.post(
                    self._base_url + path + "/extend", json=body, headers=self._headers, timeout=timeout
                )
            )

        reserved = AsyncReservation(cost, extend)
        try:
            yield reserved
        except BaseException:
            await self._settle(path + "/release", None, timeout)
            raise
        await self._settle(path + "/commit", {"cost": reserved.used}, timeout)

    async def standing(
        self, *, subject: Subject, shared: Optional[List[Subject]] = None
    ) -> Dict[str, Any]:
        """Where a subject stands on every limit on it. Not a check: nothing is counted."""
        return await self._call(_STANDING, {}, _standing_query(subject, shared))
