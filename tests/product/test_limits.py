"""Limitry's guard / hold / standing helpers, against a fake API."""

import asyncio
import json

import httpx
import pytest

USER = {"kind": "user", "id": "u1"}


def allowed(**extra):
    return {"allowed": True, "remaining": [{"failMode": "open"}], **extra}


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
        "remaining": [],
        "reason": {"code": "limit_exceeded", "limit": "AI replies per day"},
        "retryAfterSeconds": 60,
    }
    limitry, _ = api(sdk, lambda r: httpx.Response(200, json=refusal))
    ran = []
    with pytest.raises(sdk.LimitExceeded) as raised:
        with limitry.guard(subject=USER, action="ai-call"):
            ran.append(True)
    assert ran == []
    assert (raised.value.reason, raised.value.limit, raised.value.retry_after_seconds) == (
        "limit_exceeded",
        "AI replies per day",
        60,
    )


def test_unreachable_fails_open_then_closed_once_a_closed_limit_was_seen(sdk):
    # A 5xx is sent once more before it counts as unreachable.
    answers = [
        httpx.Response(503),
        httpx.Response(503),
        httpx.Response(200, json={"allowed": True, "remaining": [{"failMode": "closed"}]}),
        httpx.Response(503),
        httpx.Response(503),
    ]
    limitry, _ = api(sdk, lambda r: answers.pop(0))
    with limitry.guard(subject=USER, action="a"):
        pass  # open: no decision seen yet
    with limitry.guard(subject=USER, action="a"):
        pass
    with pytest.raises(sdk.LimitExceeded) as raised:
        with limitry.guard(subject=USER, action="a"):
            pass
    assert raised.value.reason == "unavailable"


def test_a_bad_request_is_the_callers_mistake(sdk):
    limitry, _ = api(
        sdk, lambda r: httpx.Response(400, json={"error": {"code": "invalid_request", "message": "no"}})
    )
    with pytest.raises(sdk.ApiError):
        with limitry.guard(subject=USER, action="a"):
            pass


def test_hold_commits_the_reported_cost(sdk):
    limitry, seen = api(
        sdk,
        lambda r: httpx.Response(200, json=allowed(reservation={"id": "res_1"}))
        if r.url.path.endswith("/checks")
        else httpx.Response(200, json={}),
    )
    with limitry.hold(subject=USER, action="chat", estimate=4000) as held:
        held.cost(1234)
    check, commit = seen
    assert json.loads(check.content)["mode"] == "reserve"
    assert commit.url.path == "/v1/reservations/res_1/commit"
    assert json.loads(commit.content) == {"cost": 1234}


def test_hold_releases_when_the_block_raises(sdk):
    limitry, seen = api(
        sdk,
        lambda r: httpx.Response(200, json=allowed(reservation={"id": "res_1"}))
        if r.url.path.endswith("/checks")
        else httpx.Response(200, json={}),
    )
    with pytest.raises(RuntimeError):
        with limitry.hold(subject=USER, action="chat"):
            raise RuntimeError("the model failed")
    assert seen[1].url.path == "/v1/reservations/res_1/release"


def test_standing_asks_for_the_subject_and_shared_pools(sdk):
    limitry, seen = api(sdk, lambda r: httpx.Response(200, json={"limits": []}))
    limitry.standing(subject=USER, shared=[{"kind": "team", "id": "t1"}])
    assert seen[0].url.params["shared"] == "team:t1"


def test_async_hold_commits(sdk):
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
            async with limitry.hold(subject=USER, action="chat", estimate=10) as held:
                held.cost(7)

    asyncio.run(run())
    assert json.loads(seen[1].content) == {"cost": 7}


def test_hold_extends_for_longer_work(sdk):
    limitry, seen = api(
        sdk,
        lambda r: httpx.Response(200, json=allowed(reservation={"id": "res_9"}))
        if r.url.path.endswith("/checks")
        else httpx.Response(200, json={}),
    )
    with limitry.hold(subject=USER, action="export") as held:
        held.extend(600)
    assert seen[1].url.path == "/v1/reservations/res_9/extend"
    assert json.loads(seen[1].content) == {"ttlSeconds": 600}
    assert seen[2].url.path == "/v1/reservations/res_9/commit"


def test_a_failed_check_is_sent_again_with_the_same_key(sdk):
    answers = [httpx.Response(502), httpx.Response(200, json=allowed())]
    limitry, seen = api(sdk, lambda r: answers.pop(0))
    with limitry.guard(subject=USER, action="a"):
        pass
    keys = [r.headers["idempotency-key"] for r in seen]
    assert len(keys) == 2 and keys[0] == keys[1]


def test_a_hold_granted_to_a_check_that_timed_out_is_released_after_the_block(sdk):
    checks = []

    def handler(request):
        if request.url.path.endswith("/checks"):
            checks.append(request)
            if len(checks) == 1:
                raise httpx.ReadTimeout("slow", request=request)
            return httpx.Response(200, json=allowed(reservation={"id": "rsv_late"}))
        return httpx.Response(200, json={})

    limitry, seen = api(sdk, handler)
    with limitry.hold(subject=USER, action="a", estimate=2):
        pass  # ran unmetered
    assert checks[1].headers["idempotency-key"] == checks[0].headers["idempotency-key"]
    assert checks[1].content == checks[0].content
    assert seen[-1].url.path.endswith("/reservations/rsv_late/release")
