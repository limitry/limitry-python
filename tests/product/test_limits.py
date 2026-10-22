"""Limitry's guard / reserve / standing helpers, against a fake API."""

import asyncio
import json

import httpx
import pytest

USER = {"kind": "user", "id": "u1"}


def allowed(**extra):
    return {"allowed": True, "limits": [{"failMode": "open"}], **extra}


def api(sdk, handler):
    seen = []

    def respond(request):
        seen.append(request)
        return handler(request)

    client = sdk.Limitry(token="sk_test", http_client=httpx.Client(transport=httpx.MockTransport(respond)))
    return client, seen


def test_guard_runs_the_block_when_allowed(sdk):
    limitry, seen = api(sdk, lambda r: httpx.Response(200, json=allowed()))
    ran = []
    with limitry.guard(subject=USER, action="ai-call", cost=2):
        ran.append(True)
    assert ran == [True]
    assert json.loads(seen[0].content) == {"subject": USER, "action": "ai-call", "cost": 2}


def test_guard_refuses_before_the_block(sdk):
    refusal = {
        "allowed": False,
        "limits": [],
        "reason": {"code": "limit_reached", "limitId": "lmt_a"},
        "retryAfterSeconds": 60,
    }
    limitry, _ = api(sdk, lambda r: httpx.Response(200, json=refusal))
    ran = []
    with pytest.raises(sdk.LimitReached) as raised:
        with limitry.guard(subject=USER, action="ai-call"):
            ran.append(True)
    assert ran == []
    assert not isinstance(raised.value, sdk.Unavailable)
    assert (raised.value.reason, raised.value.limit_id, raised.value.retry_after_seconds) == (
        "limit_reached",
        "lmt_a",
        60,
    )
    # An answer without the API's sentence still says what happened.
    assert str(raised.value) == "Refused"


def test_refusal_shows_the_apis_sentence(sdk):
    refusal = {
        "allowed": False,
        "limits": [
            {
                "limitId": "lmt_a",
                "name": "AI replies per day",
                "unit": "replies",
                "remaining": 0,
                "resetsAt": "2026-10-20T00:00:00.000Z",
            }
        ],
        "reason": {
            "code": "limit_reached",
            "limitId": "lmt_a",
            "message": "Refused by AI replies per day: 0 replies left. Try again in 60 s.",
        },
        "retryAfterSeconds": 60,
    }
    limitry, _ = api(sdk, lambda r: httpx.Response(200, json=refusal))
    with pytest.raises(sdk.LimitReached) as raised:
        with limitry.guard(subject=USER, action="ai-call"):
            pass
    assert str(raised.value) == "Refused by AI replies per day: 0 replies left. Try again in 60 s."


@pytest.fixture
def no_waiting(monkeypatch):
    """Backoff without the wait (and without jitter): retries happen at once, inside the budget."""
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    monkeypatch.setattr("random.random", lambda: 0.0)

    async def instant(seconds):
        return None

    monkeypatch.setattr("asyncio.sleep", instant)


def test_unreachable_fails_open_then_closed_once_a_closed_limit_was_seen(sdk, no_waiting):
    # A 5xx is retried twice, as in TypeScript, before it counts as unreachable.
    answers = [
        httpx.Response(503),
        httpx.Response(503),
        httpx.Response(503),
        httpx.Response(200, json={"allowed": True, "limits": [{"failMode": "closed"}]}),
        httpx.Response(503),
        httpx.Response(503),
        httpx.Response(503),
    ]
    limitry, _ = api(sdk, lambda r: answers.pop(0))
    with limitry.guard(subject=USER, action="a"):
        pass  # open: no decision seen yet
    with limitry.guard(subject=USER, action="a"):
        pass
    with pytest.raises(sdk.Unavailable):
        with limitry.guard(subject=USER, action="a"):
            pass


def _network_error(request):
    raise httpx.ConnectError("down", request=request)


def _too_slow(request):
    raise httpx.ReadTimeout("slow", request=request)


# Every way Limitry may not answer: the limit's fail mode decides —
# Unavailable when it fails closed, the block runs when it fails open;
# never LimitReached, which only a decision raises.
@pytest.mark.parametrize(
    "failure",
    [
        _network_error,
        _too_slow,
        lambda r: httpx.Response(500),
        lambda r: httpx.Response(402, json={"error": {"code": "plan_limit_reached"}}),
        lambda r: httpx.Response(429, json={"error": {"code": "rate_limited"}}),
    ],
    ids=["network error", "timeout", "500", "402", "429"],
)
@pytest.mark.parametrize("fail_mode", ["closed", "open"])
def test_unreachable_is_unavailable_when_closed_and_runs_when_open(sdk, no_waiting, failure, fail_mode):
    state = {"down": False}

    def handler(request):
        if state["down"]:
            return failure(request)
        return httpx.Response(200, json={"allowed": True, "limits": [{"failMode": fail_mode}]})

    limitry, _ = api(sdk, handler)
    with limitry.guard(subject=USER, action="a"):
        pass
    state["down"] = True
    ran = []
    if fail_mode == "closed":
        with pytest.raises(sdk.Unavailable) as raised:
            with limitry.guard(subject=USER, action="a"):
                ran.append(True)
        assert not isinstance(raised.value, sdk.LimitReached)
        assert ran == []
    else:
        with limitry.guard(subject=USER, action="a"):
            ran.append(True)
        assert ran == [True]


def test_a_bad_request_is_the_callers_mistake(sdk):
    limitry, _ = api(
        sdk, lambda r: httpx.Response(400, json={"error": {"code": "invalid_request", "message": "no"}})
    )
    with pytest.raises(sdk.ApiError):
        with limitry.guard(subject=USER, action="a"):
            pass


def test_reserve_commits_the_reported_cost(sdk):
    limitry, seen = api(
        sdk,
        lambda r: httpx.Response(200, json=allowed(reservation={"id": "res_1"}))
        if r.url.path.endswith("/checks")
        else httpx.Response(200, json={}),
    )
    with limitry.reserve(subject=USER, action="chat", cost=4000) as reservation:
        reservation.cost(1234)
    check, commit = seen
    assert json.loads(check.content)["mode"] == "reserve"
    assert commit.url.path == "/v1/reservations/res_1/commit"
    assert json.loads(commit.content) == {"cost": 1234}


def test_reserve_releases_when_the_block_raises(sdk):
    limitry, seen = api(
        sdk,
        lambda r: httpx.Response(200, json=allowed(reservation={"id": "res_1"}))
        if r.url.path.endswith("/checks")
        else httpx.Response(200, json={}),
    )
    with pytest.raises(RuntimeError):
        with limitry.reserve(subject=USER, action="chat"):
            raise RuntimeError("the model failed")
    assert seen[1].url.path == "/v1/reservations/res_1/release"


def test_standing_asks_for_the_subject_and_shared_pools(sdk):
    limitry, seen = api(sdk, lambda r: httpx.Response(200, json={"limits": []}))
    limitry.standing(subject=USER, shared=[{"kind": "team", "id": "t1"}])
    assert seen[0].url.params["subject"] == "user:u1"
    assert seen[0].url.params["shared"] == "team:t1"


def test_async_reserve_commits(sdk):
    seen = []

    def respond(request):
        seen.append(request)
        if request.url.path.endswith("/checks"):
            return httpx.Response(200, json=allowed(reservation={"id": "res_2"}))
        return httpx.Response(200, json={})

    async def run():
        async with sdk.AsyncLimitry(
            token="sk_test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond))
        ) as limitry:
            async with limitry.reserve(subject=USER, action="chat", cost=10) as reservation:
                reservation.cost(7)

    asyncio.run(run())
    assert json.loads(seen[1].content) == {"cost": 7}


def test_reserve_extends_for_longer_work(sdk):
    limitry, seen = api(
        sdk,
        lambda r: httpx.Response(200, json=allowed(reservation={"id": "res_9"}))
        if r.url.path.endswith("/checks")
        else httpx.Response(200, json={}),
    )
    with limitry.reserve(subject=USER, action="export") as reservation:
        reservation.extend(600)
    assert seen[1].url.path == "/v1/reservations/res_9/extend"
    assert json.loads(seen[1].content) == {"ttlSeconds": 600}
    assert seen[2].url.path == "/v1/reservations/res_9/commit"


def test_a_failed_check_is_sent_again_with_the_same_key(sdk, no_waiting):
    answers = [httpx.Response(502), httpx.Response(200, json=allowed())]
    limitry, seen = api(sdk, lambda r: answers.pop(0))
    with limitry.guard(subject=USER, action="a"):
        pass
    keys = [r.headers["idempotency-key"] for r in seen]
    assert len(keys) == 2 and keys[0] == keys[1]


def test_a_reservation_made_by_a_check_that_timed_out_is_released_after_the_block(sdk):
    checks = []

    def handler(request):
        if request.url.path.endswith("/checks"):
            checks.append(request)
            if len(checks) == 1:
                raise httpx.ReadTimeout("slow", request=request)
            return httpx.Response(200, json=allowed(reservation={"id": "rsv_late"}))
        return httpx.Response(200, json={})

    limitry, seen = api(sdk, handler)
    with limitry.reserve(subject=USER, action="a", cost=2):
        pass  # ran unmetered
    assert checks[1].headers["idempotency-key"] == checks[0].headers["idempotency-key"]
    assert checks[1].content == checks[0].content
    assert seen[-1].url.path.endswith("/reservations/rsv_late/release")


def test_a_429_is_retried_too_and_a_402_applies_the_fail_mode(sdk, no_waiting):
    # The TypeScript SDK's rule: 408, 429 and 5xx are retried; 402
    # plan_limit_reached (and a 429 that lasts) is no decision, so the fail
    # mode (open) applies.
    answers = [httpx.Response(429), httpx.Response(200, json=allowed())]
    limitry, seen = api(sdk, lambda r: answers.pop(0))
    with limitry.guard(subject=USER, action="a"):
        pass
    assert len(seen) == 2
    limitry, _ = api(sdk, lambda r: httpx.Response(402, json={"error": {"code": "plan_limit_reached"}}))
    with limitry.guard(subject=USER, action="a"):
        pass


def test_retries_stay_inside_the_check_budget(sdk, monkeypatch):
    # A wait that would outlast `timeout` is no answer in time: the fail
    # mode decides then, not after the backoff.
    slept = []
    monkeypatch.setattr("time.sleep", slept.append)
    limitry, seen = api(sdk, lambda r: httpx.Response(503, headers={"retry-after": "5"}))
    with limitry.guard(subject=USER, action="a", timeout=1.5):
        pass  # open: ran unmetered
    assert len(seen) == 1 and slept == []


@pytest.mark.parametrize("bad", [2.5, -1, 1_000_000_001, True, "3"])
def test_a_cost_the_api_would_not_commit_raises_in_the_block(sdk, bad):
    limitry, seen = api(
        sdk,
        lambda r: httpx.Response(200, json=allowed(reservation={"id": "res_1"}))
        if r.url.path.endswith("/checks")
        else httpx.Response(200, json={}),
    )
    with pytest.raises(ValueError):
        with limitry.reserve(subject=USER, action="chat") as reservation:
            reservation.cost(bad)
    # Never a commit the API would refuse: the reservation is released.
    assert [r.url.path for r in seen] == ["/v1/checks", "/v1/reservations/res_1/release"]


def test_a_whole_float_cost_commits_as_an_integer(sdk):
    limitry, seen = api(
        sdk,
        lambda r: httpx.Response(200, json=allowed(reservation={"id": "res_1"}))
        if r.url.path.endswith("/checks")
        else httpx.Response(200, json={}),
    )
    with limitry.reserve(subject=USER, action="chat") as reservation:
        reservation.cost(3.0)
    assert json.loads(seen[1].content) == {"cost": 3}


def test_extend_passes_the_ttl_as_given_and_raises_the_apis_refusal(sdk):
    def handler(request):
        if request.url.path.endswith("/checks"):
            return httpx.Response(200, json=allowed(reservation={"id": "res_9"}))
        if request.url.path.endswith("/extend"):
            return httpx.Response(400, json={"error": {"code": "invalid_request", "message": "ttlSeconds: too small"}})
        return httpx.Response(200, json={})

    limitry, seen = api(sdk, handler)
    with pytest.raises(sdk.ApiError) as raised:
        with limitry.reserve(subject=USER, action="export") as reservation:
            reservation.extend(0)
    assert json.loads(seen[1].content) == {"ttlSeconds": 0}
    assert raised.value.status == 400


def test_settles_retry_with_one_key(sdk, no_waiting):
    answers = {"commit": [httpx.Response(503), httpx.Response(200, json={})]}

    def handler(request):
        if request.url.path.endswith("/checks"):
            return httpx.Response(200, json=allowed(reservation={"id": "res_1"}))
        return answers["commit"].pop(0)

    limitry, seen = api(sdk, handler)
    with limitry.reserve(subject=USER, action="chat") as reservation:
        reservation.cost(2)
    commits = [r for r in seen if r.url.path.endswith("/commit")]
    assert len(commits) == 2
    assert commits[0].headers["idempotency-key"] == commits[1].headers["idempotency-key"]


def test_standing_retries_and_reports_a_non_json_error(sdk, no_waiting):
    answers = [httpx.Response(502), httpx.Response(200, json={"limits": []})]
    limitry, seen = api(sdk, lambda r: answers.pop(0))
    assert limitry.standing(subject=USER) == {"limits": []}
    assert len(seen) == 2
    limitry, _ = api(sdk, lambda r: httpx.Response(404, text="not json"))
    with pytest.raises(sdk.ApiError) as raised:
        limitry.standing(subject=USER)
    assert raised.value.status == 404


def test_async_cost_is_checked_too(sdk):
    def respond(request):
        if request.url.path.endswith("/checks"):
            return httpx.Response(200, json=allowed(reservation={"id": "res_2"}))
        return httpx.Response(200, json={})

    async def run():
        async with sdk.AsyncLimitry(
            token="sk_test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond))
        ) as limitry:
            async with limitry.reserve(subject=USER, action="chat") as reservation:
                reservation.cost(-1)

    with pytest.raises(ValueError):
        asyncio.run(run())


def moving():
    return httpx.Response(503, json={"error": {"code": "moving"}}, headers={"retry-after": "1"})


def test_moving_is_asked_again_with_the_same_key_outside_the_budget(sdk, monkeypatch):
    # A subject being handed over: reached, nothing counted — not
    # "unreachable". Each attempt gets a fresh budget; the block is counted.
    slept = []
    monkeypatch.setattr("time.sleep", slept.append)
    monkeypatch.setattr("random.random", lambda: 0.0)
    answers = [moving(), moving(), moving(), httpx.Response(200, json=allowed())]
    limitry, seen = api(sdk, lambda r: answers.pop(0))
    ran = []
    with limitry.guard(subject=USER, action="a", timeout=1.5):
        ran.append(True)
    assert ran == [True] and len(seen) == 4 and slept == [1.0, 1.0, 1.0]
    assert len({r.headers["idempotency-key"] for r in seen}) == 1


def test_moving_for_30_seconds_lets_the_fail_mode_decide(sdk, no_waiting):
    limitry, seen = api(sdk, lambda r: moving())
    with limitry.guard(subject=USER, action="a"):
        pass  # open: ran unmetered, but only after 30 s
    assert len(seen) == 31
    with pytest.raises(sdk.Unavailable):
        with limitry.guard(subject=USER, action="a", fail_mode="closed"):
            pass


def test_a_commit_keeps_going_through_a_handover(sdk, no_waiting):
    commits = []

    def handler(request):
        if request.url.path.endswith("/checks"):
            return httpx.Response(200, json=allowed(reservation={"id": "rsv_m"}))
        commits.append(request)
        return moving() if len(commits) < 3 else httpx.Response(200, json={})

    limitry, _ = api(sdk, handler)
    with limitry.reserve(subject=USER, action="a", cost=2):
        pass
    assert len(commits) == 3
    assert len({r.headers["idempotency-key"] for r in commits}) == 1


def test_async_moving_is_asked_again(sdk, no_waiting):
    answers = [moving(), httpx.Response(200, json=allowed())]

    async def run():
        client = sdk.AsyncLimitry(
            token="sk_test",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: answers.pop(0))),
        )
        async with client.guard(subject=USER, action="a"):
            return True

    assert asyncio.run(run())


def test_moving_with_retry_after_zero_waits_a_second_and_ends(sdk, monkeypatch):
    slept = []
    monkeypatch.setattr("time.sleep", slept.append)
    monkeypatch.setattr("random.random", lambda: 0.0)
    limitry, seen = api(
        sdk,
        lambda r: httpx.Response(503, json={"error": {"code": "moving"}}, headers={"retry-after": "0"}),
    )
    with limitry.guard(subject=USER, action="a"):
        pass
    assert len(seen) == 31 and all(s == 1.0 for s in slept)


def busy(seconds="2"):
    return httpx.Response(503, json={"error": {"code": "busy"}}, headers={"retry-after": seconds})


def test_busy_is_asked_again_after_retry_after_plus_jitter(sdk, monkeypatch):
    # An overloaded limiter: reached, nothing counted — retried with the
    # same key, waiting Retry-After plus a jittered, growing backoff.
    slept = []
    monkeypatch.setattr("time.sleep", slept.append)
    monkeypatch.setattr("random.random", lambda: 0.5)
    answers = [busy(), busy(), busy(), httpx.Response(200, json=allowed())]
    limitry, seen = api(sdk, lambda r: answers.pop(0))
    ran = []
    with limitry.guard(subject=USER, action="a", timeout=1.5):
        ran.append(True)
    assert ran == [True] and len(seen) == 4
    assert slept == [2.125, 2.25, 2.5]
    assert len({r.headers["idempotency-key"] for r in seen}) == 1


def test_busy_shares_movings_30_seconds_then_the_fail_mode_decides(sdk, monkeypatch):
    slept = []
    monkeypatch.setattr("time.sleep", slept.append)
    flip = []

    def handler(request):
        flip.append(1)
        return busy("1") if len(flip) % 2 else moving()

    limitry, seen = api(sdk, handler)
    with limitry.guard(subject=USER, action="a"):
        pass  # open: ran unmetered, but only after 30 s
    assert sum(slept) <= 30.0 and len(seen) > 5
    for n, s in enumerate(slept):
        assert 1.0 <= s < 1.0 + min(4.0, 0.25 * 2**n)
    with pytest.raises(sdk.Unavailable):
        with limitry.guard(subject=USER, action="a", fail_mode="closed"):
            pass


def test_async_busy_is_asked_again(sdk, no_waiting):
    answers = [busy(), httpx.Response(200, json=allowed())]

    async def run():
        client = sdk.AsyncLimitry(
            token="sk_test",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: answers.pop(0))),
        )
        async with client.guard(subject=USER, action="a"):
            return True

    assert asyncio.run(run()) and answers == []
