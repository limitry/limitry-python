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
#         reservation.cost = price_cents(answer.usage)
#
# A refusal raises `LimitReachedError` (with `limit_id` and
# `retry_after_seconds`). When Limitry can't be reached — an error, a
# 5xx, no answer within `timeout`, the workspace past its plan's
# allowance (402) or the edge's flood guard (429) — each limit's fail
# mode decides, as in the TypeScript SDK: the last decision's fail mode
# for this subject and action is remembered, and `closed` raises
# `UnavailableError` (its `reason` says which, and `__cause__` what
# failed) while `open`, the default, runs the block uncounted. One
# exception, as in the TypeScript SDK: an `open` concurrent limit is
# held to its amount in this process (design-fallback.md). `reserve`
# counts the blocks it has going per subject and action, answered or
# not, and remembers the smallest concurrent limit's `amount` from the
# last decision; while Limitry can't answer, a reserve that would pass
# it raises `UnavailableError`. The count is this process's only:
# several servers may together run more, bounded where it was unbounded.
#
# `timeout` (seconds, 1.5 by default) is the most a check adds to your
# request, its tries included: the fail-open promise, and not the
# client's per-attempt `timeout`. Cancelling an async guard's or
# reserve's task (`asyncio.timeout()` around it) stops the check at once,
# waits included: the block never runs and no fail mode applies, since
# cancelling is not Limitry being unreachable. A reserve cancelled while
# its check is out releases any reservation the check made, in a
# background task.
#
# Retries are safe, and follow the TypeScript SDK's rule exactly: each
# guard or reserve sends one Idempotency-Key (yours, `idempotency_key=`,
# else a new one), and a check is retried with it (a network error, 408,
# 429, 5xx; at most twice, the client's backoff) all within ONE time
# budget, `timeout` — so it counts once and never waits longer than
# that. A reservation whose check timed out may have been made all the
# same: once the block is done, `reserve` sends the check again with the
# same key (a replay, nothing counted twice) and releases the
# reservation it finds. A guard whose check timed out finishes it the
# same way, once its block is done: sent again with the same key ONLY TO
# FINISH it (`limitry-finish-only`), so a check part way across Limitry's
# servers completes and one that never arrived charges nothing. Python
# does it after the block, in the caller's own thread or task and within
# `timeout`, never in a background thread that an exiting interpreter
# would drop. Commits and releases retry the same way within the same
# `timeout`.
#
# A subject being handed over between Limitry's servers (a split, a few
# seconds) answers `503 moving`, and a limiter with too many checks
# inside at once `503 busy`: Limitry was reached and charged nothing. The
# check, commit or release is sent again with the same key only while the
# wait still fits in `timeout` — the wait is the answer's Retry-After (at
# least a second) plus full jitter on a growing backoff, the TypeScript
# SDK's `retryWaitMs` — and then the fail mode applies.

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
    Mapping,
    Optional,
    Set,
    TypedDict,
)

import httpx

from .._error import ProductError
from .._exceptions import error_for
from .._models import construct
from .._runtime import (
    DEFAULT_MAX_RETRIES,
    Operation,
    _backoff,
    _retryable,
    _wait,
)
from ..types import CheckDecision, SubjectStanding


class Subject(TypedDict):
    kind: str
    id: str


FailMode = Literal["open", "closed"]
UnavailableReason = Literal["timeout", "unreachable", "plan_limit_reached", "rate_limited"]
# The backoff added to a `moving` or `busy` wait: full jitter under base × 2^n, capped (seconds).
_JITTER_BASE = 0.25
_JITTER_CAP = 4.0
_MEMORY = 10_000
# The largest cost a commit takes.
_MAX_COST = 1_000_000_000
_STANDING: Operation = {
    "method": "GET",
    "path": "/subjects/{subjectKind}/{subjectId}/standing",
    "query": ["shared"],
    "body": False,
    "names": {},
}
_EXTEND: Operation = {
    "method": "POST",
    "path": "/reservations/{reservationId}/extend",
    "query": [],
    "body": True,
    "names": {},
}


class LimitReachedError(ProductError):
    """Refused: a limit is reached, or the subject holds too many open reservations."""

    def __init__(self, decision: CheckDecision) -> None:
        # Read leniently, as the models are: a field missing is None.
        reason = getattr(decision, "reason", None)
        # The API's own sentence (reason.message), one wording everywhere.
        super().__init__(getattr(reason, "message", None) or "Refused")
        self.reason: str = getattr(reason, "code", None) or "limit_reached"
        """`limit_reached` or `too_many_reservations`."""
        self.limit_id: Optional[str] = getattr(reason, "limit_id", None)
        """The limit that refused; None for too many open reservations."""
        self.limit_name: Optional[str] = getattr(reason, "limit_name", None)
        self.retry_after_seconds: Optional[int] = getattr(decision, "retry_after_seconds", None)
        self.decision: CheckDecision = decision


class UnavailableError(ProductError):
    """Limitry gave no decision (`reason` says why, `__cause__` what failed)
    and a limit on this subject and action fails closed. With `open`, the
    block runs instead."""

    def __init__(self, reason: UnavailableReason) -> None:
        super().__init__(f"Limitry gave no decision ({reason}) and a limit refuses when unreachable")
        self.reason: UnavailableReason = reason
        """`timeout`, `unreachable`, `plan_limit_reached` or `rate_limited`."""


class _NoDecision:
    """Why there is no decision, and what failed."""

    def __init__(self, reason: UnavailableReason, cause: Optional[BaseException] = None) -> None:
        self.reason: UnavailableReason = reason
        self.cause = cause

    @property
    def timed_out(self) -> bool:
        """No answer in time: unlike an error, the check may have been decided."""
        return self.reason == "timeout"

    def error(self) -> UnavailableError:
        error = UnavailableError(self.reason)
        error.__cause__ = self.cause
        return error


def _checked_cost(units: Any) -> int:
    if (
        isinstance(units, bool)
        or not isinstance(units, (int, float))
        or not float(units).is_integer()
        or not 0 <= units <= _MAX_COST
    ):
        raise ValueError(f"reservation.cost = {units!r}: the cost must be a whole number from 0 to {_MAX_COST}")
    return int(units)


class Reservation:
    """A reservation in progress: set what the work used, keep it for longer work."""

    def __init__(self, reserved: int, extend: Any = None) -> None:
        self.reserved = reserved
        """What was reserved."""
        self._cost = reserved
        self._extend = extend

    @property
    def cost(self) -> int:
        """The cost actually used, committed when the block ends. Default: the reserved cost.

        A whole number from 0 to 1,000,000,000, as the API commits:
        anything else raises ValueError here, in your code, rather than a
        commit the API refuses after the work (whose usage would go
        uncounted)."""
        return self._cost

    @cost.setter
    def cost(self, units: int) -> None:
        self._cost = _checked_cost(units)

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
    cost: int,
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


def _decision(response: Any) -> Any:
    """The decision (a CheckDecision), or why there is none: 402 past the
    plan, 429 the flood guard, 5xx; any other error is the caller's
    mistake, raised as the API's error."""
    if isinstance(response, _NoDecision):
        return response
    if response.status_code < 300:
        return construct(CheckDecision, response.json())
    if response.status_code == 402:
        return _NoDecision("plan_limit_reached", error_for(response))
    if response.status_code == 429:
        return _NoDecision("rate_limited", error_for(response))
    if response.status_code >= 500:
        return _NoDecision("unreachable", error_for(response))
    raise error_for(response)


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


def _standing_path(subject: Subject) -> Dict[str, Any]:
    return {"subjectKind": subject["kind"], "subjectId": subject["id"]}


def _standing_query(shared: Optional[List[Subject]]) -> Dict[str, Any]:
    query: Dict[str, Any] = {}
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

    def mode(self, subject: Subject, action: str, forced: Optional[FailMode]) -> FailMode:
        return forced or self._seen.get((subject["kind"], subject["id"], action), "open")

    def remember(self, subject: Subject, action: str, decision: CheckDecision) -> None:
        key = (subject["kind"], subject["id"], action)
        # Read leniently, as the models are: a field missing is no fact.
        amounts = [
            r.amount
            for r in decision.limits
            if getattr(r, "window", None) == "concurrent" and isinstance(getattr(r, "amount", None), int)
        ]
        if amounts:
            self._amounts[key] = min(amounts)
            if len(self._amounts) > _MEMORY:
                self._amounts.popitem(last=False)
        else:
            self._amounts.pop(key, None)
        closed = any(getattr(r, "fail_mode", None) == "closed" for r in decision.limits)
        self._seen[key] = "closed" if closed else "open"
        if len(self._seen) > _MEMORY:
            self._seen.popitem(last=False)


def _memory_of(client: Any) -> _Memory:
    memory = client.__dict__.get("_limitry_memory")
    if memory is None:
        memory = client.__dict__["_limitry_memory"] = _Memory()
    return memory


class ProductMethods:
    """`limitry.guard(...)`, `limitry.reserve(...)` and `limitry.standing(...)`."""

    _http: httpx.Client
    _origin: str
    _headers: httpx.Headers
    _call: Any

    def _send(
        self,
        path: str,
        body: Any,
        key: str,
        budget: float,
        extra_headers: Optional[Mapping[str, str]] = None,
        finish_only: bool = False,
    ) -> Any:
        """The answer — or a _NoDecision: the budget ran out ("timeout"), or a
        network error on the last try ("unreachable"). The client's retry
        rule, inside one time budget: the TypeScript SDK's `check` and
        `settle`."""
        headers = httpx.Headers(self._headers)
        headers.update(extra_headers or {})
        headers["idempotency-key"] = key
        if finish_only:
            headers["limitry-finish-only"] = "1"
        left = _Budget(budget)
        attempt = 0
        moving_tries = 0
        while True:
            remaining = left()
            if remaining <= 0:
                return _NoDecision("timeout")
            last = attempt >= getattr(self, "_max_retries", DEFAULT_MAX_RETRIES)
            try:
                response = self._http.post(self._origin + "/v1" + path, json=body, headers=headers, timeout=remaining)
            except httpx.TimeoutException as error:
                return _NoDecision("timeout", error)
            except httpx.HTTPError as error:
                if last:
                    return _NoDecision("unreachable", error)
                delay: Optional[float] = _backoff(attempt)
            else:
                moving = _moving(response, moving_tries)
                if moving is not None:
                    moving_tries += 1
                    # Reached, nothing counted: asked again only while the wait fits.
                    if moving >= left():
                        return _NoDecision("unreachable", error_for(response))
                    left.slept += moving
                    time.sleep(moving)
                    continue
                delay = None if last or not _retryable(response) else _wait(response, attempt)
                if delay is None:
                    return response
            if delay >= left():
                return _NoDecision("timeout")
            left.slept += delay
            time.sleep(delay)
            attempt += 1

    def _check(
        self,
        body: Dict[str, Any],
        timeout: float,
        key: str,
        extra_headers: Optional[Mapping[str, str]] = None,
        finish_only: bool = False,
    ) -> Any:
        return _decision(self._send("/checks", body, key, timeout, extra_headers, finish_only))

    def _finish(self, body: Dict[str, Any], key: str, timeout: float, extra_headers: Any) -> None:
        # After a guard's check timed out and its block ran: the same key,
        # only to finish it (a check never decided afresh). Never fails
        # the caller's work.
        try:
            self._check(body, timeout, key, extra_headers, finish_only=True)
        except ProductError:
            pass

    def _release_unheard(self, body: Dict[str, Any], key: str, timeout: float, extra_headers: Any) -> None:
        # After a reservation's check timed out: ask again with the same key
        # (a replay if Limitry decided it) and release any reservation it made.
        try:
            replay = self._check(body, timeout, key, extra_headers)
        except ProductError:
            return
        reservation = getattr(replay, "reservation", None) if isinstance(replay, CheckDecision) else None
        if reservation:
            self._settle(f"/reservations/{reservation.id}/release", None, timeout, extra_headers)

    def _settle(self, path: str, body: Optional[Dict[str, Any]], timeout: float, extra_headers: Any) -> None:
        # Committing or releasing never fails the caller's work: it is
        # retried (the same commit answers the same) within `timeout`, as
        # it runs before the block's caller goes on; a lost one expires by
        # itself (an expired reservation is not charged).
        self._send(path, body, str(uuid.uuid4()), timeout, extra_headers)

    @contextmanager
    def guard(
        self,
        *,
        subject: Subject,
        action: str,
        cost: int = 1,
        shared: Optional[List[Subject]] = None,
        timeout: float = 1.5,
        fail_mode: Optional[FailMode] = None,
        idempotency_key: Optional[str] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> Iterator[None]:
        """Checks `cost`, and runs the block only if allowed (else raises
        `LimitReachedError`). `timeout` is the most the check adds, in
        seconds, its tries included."""
        memory = _memory_of(self)
        body = _check_body(subject, action, cost, shared)
        key = idempotency_key or str(uuid.uuid4())
        decision = self._check(body, timeout, key, extra_headers)
        if isinstance(decision, _NoDecision):
            if memory.mode(subject, action, fail_mode) == "closed":
                raise decision.error()
            try:
                yield
            finally:
                if decision.timed_out:
                    self._finish(body, key, timeout, extra_headers)
            return
        memory.remember(subject, action, decision)
        if not decision.allowed:
            raise LimitReachedError(decision)
        yield

    @contextmanager
    def reserve(
        self,
        *,
        subject: Subject,
        action: str,
        cost: int = 1,
        shared: Optional[List[Subject]] = None,
        ttl_seconds: Optional[int] = None,
        timeout: float = 1.5,
        fail_mode: Optional[FailMode] = None,
        idempotency_key: Optional[str] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> Iterator[Reservation]:
        """Reserves `cost`, runs the block, then commits the cost it set
        (`reservation.cost = n`, default the reserved cost) — or releases the
        reservation if the block raises. A concurrent limit's slot is taken
        exactly as long as the block runs."""
        memory = _memory_of(self)
        before = memory.start(subject, action)
        try:
            with self._reserve(
                memory, before, subject, action, cost, shared, ttl_seconds, timeout, fail_mode, idempotency_key, extra_headers
            ) as r:
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
        cost: int,
        shared: Optional[List[Subject]],
        ttl_seconds: Optional[int],
        timeout: float,
        fail_mode: Optional[FailMode],
        idempotency_key: Optional[str],
        extra_headers: Optional[Mapping[str, str]],
    ) -> Iterator[Reservation]:
        body = _check_body(subject, action, cost, shared, True, ttl_seconds)
        key = idempotency_key or str(uuid.uuid4())
        decision = self._check(body, timeout, key, extra_headers)
        if isinstance(decision, _NoDecision):
            timed_out = decision.timed_out
            # Open, but never past a concurrent limit's amount here.
            if memory.mode(subject, action, fail_mode) == "closed" or memory.held(subject, action, before):
                if timed_out:
                    self._release_unheard(body, key, timeout, extra_headers)
                raise decision.error()
            try:
                yield Reservation(cost)
            finally:
                if timed_out:
                    self._release_unheard(body, key, timeout, extra_headers)
            return
        memory.remember(subject, action, decision)
        reservation = getattr(decision, "reservation", None)
        if not decision.allowed or not reservation:
            raise LimitReachedError(decision)
        path = f"/reservations/{reservation.id}"

        def extend(ttl: Optional[int]) -> None:
            # The client's own call: its retries, its key, its errors. Passed
            # as given: the API refuses a bad one (0, past 3600).
            self._call(
                _EXTEND,
                {"reservationId": reservation.id},
                {} if ttl is None else {"ttlSeconds": ttl},
                timeout=timeout,
                extra_headers=extra_headers,
            )

        reserved = Reservation(cost, extend)
        try:
            yield reserved
        except BaseException:
            self._settle(path + "/release", None, timeout, extra_headers)
            raise
        self._settle(path + "/commit", {"cost": reserved.cost}, timeout, extra_headers)

    def standing(
        self,
        *,
        subject: Subject,
        shared: Optional[List[Subject]] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
        timeout: Any = None,
    ) -> SubjectStanding:
        """Where a subject stands on every limit on it: used, remaining,
        resets_at — for showing an end user. Not a check: nothing is
        counted."""
        # The client's own call: its retries, and its errors (any body).
        return self._call(
            _STANDING,
            _standing_path(subject),
            _standing_query(shared),
            cast_to=SubjectStanding,
            extra_headers=extra_headers,
            **({} if timeout is None else {"timeout": timeout}),
        )


class AsyncProductMethods:
    """The same, to `async with` / `await`."""

    _http: httpx.AsyncClient
    _origin: str
    _headers: httpx.Headers
    _call: Any

    def _keep(self, work: Any) -> None:
        """A background task, held until it ends (asyncio keeps only a weak reference)."""
        tasks: Set["asyncio.Task[Any]"] = self.__dict__.setdefault("_limitry_tasks", set())
        task = asyncio.ensure_future(work)
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    async def _send(
        self,
        path: str,
        body: Any,
        key: str,
        budget: float,
        extra_headers: Optional[Mapping[str, str]] = None,
        finish_only: bool = False,
    ) -> Any:
        """The answer, or a _NoDecision (see ProductMethods._send)."""
        headers = httpx.Headers(self._headers)
        headers.update(extra_headers or {})
        headers["idempotency-key"] = key
        if finish_only:
            headers["limitry-finish-only"] = "1"
        left = _Budget(budget)
        attempt = 0
        moving_tries = 0
        while True:
            remaining = left()
            if remaining <= 0:
                return _NoDecision("timeout")
            last = attempt >= getattr(self, "_max_retries", DEFAULT_MAX_RETRIES)
            try:
                response = await self._http.post(
                    self._origin + "/v1" + path, json=body, headers=headers, timeout=remaining
                )
            except httpx.TimeoutException as error:
                return _NoDecision("timeout", error)
            except httpx.HTTPError as error:
                if last:
                    return _NoDecision("unreachable", error)
                delay: Optional[float] = _backoff(attempt)
            else:
                moving = _moving(response, moving_tries)
                if moving is not None:
                    moving_tries += 1
                    # Reached, nothing counted: asked again only while the wait fits.
                    if moving >= left():
                        return _NoDecision("unreachable", error_for(response))
                    left.slept += moving
                    await asyncio.sleep(moving)
                    continue
                delay = None if last or not _retryable(response) else _wait(response, attempt)
                if delay is None:
                    return response
            if delay >= left():
                return _NoDecision("timeout")
            left.slept += delay
            await asyncio.sleep(delay)
            attempt += 1

    async def _check(
        self,
        body: Dict[str, Any],
        timeout: float,
        key: str,
        extra_headers: Optional[Mapping[str, str]] = None,
        finish_only: bool = False,
    ) -> Any:
        return _decision(await self._send("/checks", body, key, timeout, extra_headers, finish_only))

    async def _finish(self, body: Dict[str, Any], key: str, timeout: float, extra_headers: Any) -> None:
        try:
            await self._check(body, timeout, key, extra_headers, finish_only=True)
        except ProductError:
            pass

    async def _release_unheard(self, body: Dict[str, Any], key: str, timeout: float, extra_headers: Any) -> None:
        # After a reservation's check timed out (or was cancelled): ask again
        # with the same key (a replay if Limitry decided it) and release any
        # reservation it made.
        try:
            replay = await self._check(body, timeout, key, extra_headers)
        except ProductError:
            return
        reservation = getattr(replay, "reservation", None) if isinstance(replay, CheckDecision) else None
        if reservation:
            await self._settle(f"/reservations/{reservation.id}/release", None, timeout, extra_headers)

    async def _settle(self, path: str, body: Optional[Dict[str, Any]], timeout: float, extra_headers: Any) -> None:
        await self._send(path, body, str(uuid.uuid4()), timeout, extra_headers)

    @asynccontextmanager
    async def guard(
        self,
        *,
        subject: Subject,
        action: str,
        cost: int = 1,
        shared: Optional[List[Subject]] = None,
        timeout: float = 1.5,
        fail_mode: Optional[FailMode] = None,
        idempotency_key: Optional[str] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> AsyncIterator[None]:
        """Checks `cost`, and runs the block only if allowed (else raises
        `LimitReachedError`). Cancelling the task stops the check at once."""
        memory = _memory_of(self)
        body = _check_body(subject, action, cost, shared)
        key = idempotency_key or str(uuid.uuid4())
        decision = await self._check(body, timeout, key, extra_headers)
        if isinstance(decision, _NoDecision):
            if memory.mode(subject, action, fail_mode) == "closed":
                raise decision.error()
            try:
                yield
            finally:
                if decision.timed_out:
                    await self._finish(body, key, timeout, extra_headers)
            return
        memory.remember(subject, action, decision)
        if not decision.allowed:
            raise LimitReachedError(decision)
        yield

    @asynccontextmanager
    async def reserve(
        self,
        *,
        subject: Subject,
        action: str,
        cost: int = 1,
        shared: Optional[List[Subject]] = None,
        ttl_seconds: Optional[int] = None,
        timeout: float = 1.5,
        fail_mode: Optional[FailMode] = None,
        idempotency_key: Optional[str] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> AsyncIterator[AsyncReservation]:
        """Reserves `cost`, runs the block, then commits what it used — or releases on an error."""
        memory = _memory_of(self)
        before = memory.start(subject, action)
        try:
            async with self._reserve(
                memory, before, subject, action, cost, shared, ttl_seconds, timeout, fail_mode, idempotency_key, extra_headers
            ) as r:
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
        cost: int,
        shared: Optional[List[Subject]],
        ttl_seconds: Optional[int],
        timeout: float,
        fail_mode: Optional[FailMode],
        idempotency_key: Optional[str],
        extra_headers: Optional[Mapping[str, str]],
    ) -> AsyncIterator[AsyncReservation]:
        body = _check_body(subject, action, cost, shared, True, ttl_seconds)
        key = idempotency_key or str(uuid.uuid4())
        try:
            decision = await self._check(body, timeout, key, extra_headers)
        except asyncio.CancelledError:
            # Cancelled while the check was out: it may have reserved all
            # the same. Released in a background task, so the cancel stays
            # at once.
            self._keep(self._release_unheard(body, key, timeout, extra_headers))
            raise
        if isinstance(decision, _NoDecision):
            timed_out = decision.timed_out
            # Open, but never past a concurrent limit's amount here.
            if memory.mode(subject, action, fail_mode) == "closed" or memory.held(subject, action, before):
                if timed_out:
                    await self._release_unheard(body, key, timeout, extra_headers)
                raise decision.error()
            try:
                yield AsyncReservation(cost)
            finally:
                if timed_out:
                    await self._release_unheard(body, key, timeout, extra_headers)
            return
        memory.remember(subject, action, decision)
        reservation = getattr(decision, "reservation", None)
        if not decision.allowed or not reservation:
            raise LimitReachedError(decision)
        path = f"/reservations/{reservation.id}"

        async def extend(ttl: Optional[int]) -> None:
            # The client's own call: its retries, its key, its errors.
            await self._call(
                _EXTEND,
                {"reservationId": reservation.id},
                {} if ttl is None else {"ttlSeconds": ttl},
                timeout=timeout,
                extra_headers=extra_headers,
            )

        reserved = AsyncReservation(cost, extend)
        try:
            yield reserved
        except BaseException:
            await self._settle(path + "/release", None, timeout, extra_headers)
            raise
        await self._settle(path + "/commit", {"cost": reserved.cost}, timeout, extra_headers)

    async def standing(
        self,
        *,
        subject: Subject,
        shared: Optional[List[Subject]] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
        timeout: Any = None,
    ) -> SubjectStanding:
        """Where a subject stands on every limit on it. Not a check: nothing is counted."""
        return await self._call(
            _STANDING,
            _standing_path(subject),
            _standing_query(shared),
            cast_to=SubjectStanding,
            extra_headers=extra_headers,
            **({} if timeout is None else {"timeout": timeout}),
        )
