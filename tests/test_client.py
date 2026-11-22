"""The runtime and the generated methods, against a fake API (httpx's mock transport)."""

import asyncio
import datetime
import json
from typing import List, Literal, Optional

import httpx
import pydantic
import pytest


def fake(handler):
    seen = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return httpx.MockTransport(respond), seen


def client(sdk, names, handler, **kwargs):
    transport, seen = fake(handler)
    cls = getattr(sdk, names["client"])
    return cls(token="sk_test", http_client=httpx.Client(transport=transport), **kwargs), seen


def async_client(sdk, names, handler, **kwargs):
    transport, seen = fake(handler)
    cls = getattr(sdk, "Async" + names["client"])
    return cls(token="sk_test", http_client=httpx.AsyncClient(transport=transport), **kwargs), seen


ENDPOINT = {
    "id": "we_1",
    "url": "https://example.com/h",
    "description": None,
    "enabled": True,
    "eventTypes": None,
    "environment": None,
    "consecutiveFailures": 0,
    "lastStatus": None,
    "lastAttemptAt": None,
    "createdAt": "2026-10-11T12:00:00.000Z",
}
MEMBER = {
    "id": "mem_1",
    "userId": "usr_1",
    "name": "Ada",
    "email": "ada@example.com",
    "role": "owner",
    "joinedAt": "2026-10-11T12:00:00.000Z",
}


def test_sends_the_key_the_body_and_an_idempotency_key(sdk, names):
    api, seen = client(sdk, names, lambda r: httpx.Response(201, json=ENDPOINT))
    api.workspace.webhook_endpoints.create(url="https://example.com/h", idempotency_key="k1")
    request = seen[0]
    assert request.method == "POST"
    assert request.url.path == "/v1/workspace/webhook-endpoints"
    assert request.headers["authorization"] == "Bearer sk_test"
    assert request.headers["idempotency-key"] == "k1"
    # The package's own version: 0.0.0.dev0 in CI, the release's in a release build.
    agent = request.headers["user-agent"]
    assert agent.endswith(f"-python/{sdk.__version__}") and " " not in agent
    assert json.loads(request.content) == {"url": "https://example.com/h"}


def test_path_parameters_query_fields_and_python_names(sdk, names):
    api, seen = client(sdk, names, lambda r: httpx.Response(200, json={"items": [], "nextCursor": None}))
    api.workspace.webhook_deliveries.list("we 1", limit=10)
    url = seen[0].url
    assert url.raw_path.split(b"?")[0] == b"/v1/workspace/webhook-endpoints/we%201/deliveries"
    assert url.params["limit"] == "10"
    assert seen[0].content == b""


def test_left_out_arguments_are_not_sent(sdk, names):
    api, seen = client(sdk, names, lambda r: httpx.Response(201, json={}))
    api.workspace.webhook_endpoints.create(url="https://example.com/h")
    assert json.loads(seen[0].content) == {"url": "https://example.com/h"}


# ── Models ───────────────────────────────────────────────────────────────


def test_a_response_is_a_model_with_python_names(sdk, names):
    api, _ = client(sdk, names, lambda r: httpx.Response(200, json=ENDPOINT))
    endpoint = api.workspace.webhook_endpoints.get("we_1")
    assert isinstance(endpoint, sdk.BaseModel) and isinstance(endpoint, sdk.types.WebhookEndpoint)
    assert (endpoint.consecutive_failures, endpoint.event_types, endpoint.enabled) == (0, None, True)
    # to_dict() and to_json() give the API's names back, as it sent them.
    assert set(endpoint.to_dict()) == set(ENDPOINT)
    assert endpoint.to_dict()["consecutiveFailures"] == 0 and endpoint.to_dict()["eventTypes"] is None
    assert json.loads(endpoint.to_json())["consecutiveFailures"] == 0


def test_a_field_or_value_the_api_adds_later_never_fails(sdk, names):
    added = {**MEMBER, "role": "auditor", "seatType": "full"}
    del added["joinedAt"]
    api, _ = client(sdk, names, lambda r: httpx.Response(200, json={"items": [added], "nextCursor": None}))
    (member,) = api.workspace.members.list().items
    assert member.role == "auditor"
    assert getattr(member, "seatType") == "full"  # kept, by its API name
    assert member.to_dict()["seatType"] == "full"
    assert "joinedAt" not in member.to_dict()


def test_inline_objects_are_models_too(sdk, names):
    event = {
        "id": "aud_1",
        "action": "workspace.member.invited",
        "actorType": "user",
        "actor": {"id": "usr_1"},
        "credential": {"id": "key_1", "type": "api_key", "name": "ci"},
        "createdAt": "2026-10-11T12:00:00.000Z",
    }
    api, _ = client(sdk, names, lambda r: httpx.Response(200, json={"items": [event], "nextCursor": None}))
    (audit,) = api.workspace.audit_events.list()
    assert isinstance(audit.actor, sdk.BaseModel) and audit.actor.id == "usr_1"
    assert audit.credential is not None and audit.credential.name == "ci"
    assert audit.actor_type == "user"


def runtime_module(sdk, name):
    return __import__(f"{sdk.__name__}.{name}", fromlist=[name])


def test_construction_parses_datetimes_and_keeps_what_does_not_fit(sdk):
    models = runtime_module(sdk, "_models")

    class Child(sdk.BaseModel):
        name: str

    class Thing(sdk.BaseModel):
        created_at: datetime.datetime = pydantic.Field(alias="createdAt")
        kind: Literal["a", "b"]
        children: List[Child]
        note: Optional[str] = None

    thing = models.construct(Thing, {"createdAt": "2026-10-11T12:00:00Z", "kind": "c", "children": [{"name": "x"}, 5]})
    assert thing.created_at == datetime.datetime(2026, 10, 11, 12, tzinfo=datetime.timezone.utc)
    assert thing.kind == "c"
    assert isinstance(thing.children[0], Child) and thing.children[1] == 5
    assert thing.note is None
    assert thing.to_dict(mode="json") == {
        "createdAt": "2026-10-11T12:00:00Z",
        "kind": "c",
        "children": [{"name": "x"}, 5],
    }
    odd = models.construct(Thing, {"createdAt": "yesterday", "kind": "a", "children": []})
    assert odd.created_at == "yesterday"


# ── Errors ───────────────────────────────────────────────────────────────


def failing(status):
    return lambda r: httpx.Response(status, json={"error": {"code": "x", "message": "No", "requestId": "req_1"}})


@pytest.mark.parametrize(
    "status,name",
    [
        (400, "BadRequestError"),
        (401, "AuthenticationError"),
        (402, "PlanLimitError"),
        (403, "PermissionDeniedError"),
        (404, "NotFoundError"),
        (409, "ConflictError"),
        (422, "UnprocessableEntityError"),
        (429, "RateLimitError"),
        (501, "InternalServerError"),
        (418, "APIError"),
    ],
)
def test_each_status_raises_its_error(sdk, names, status, name):
    api, _ = client(sdk, names, failing(status), max_retries=0)
    with pytest.raises(getattr(sdk, name)) as raised:
        api.workspace.get()
    error = raised.value
    assert type(error).__name__ == name
    assert isinstance(error, sdk.APIError) and isinstance(error, getattr(sdk, names["client"] + "Error"))
    assert (error.status_code, error.code, error.message, error.request_id) == (status, "x", "No", "req_1")


def test_an_api_error_keeps_details_docs_and_headers(sdk, names):
    details = [{"path": "url", "message": "Must be https"}]
    api, _ = client(
        sdk,
        names,
        lambda r: httpx.Response(
            400,
            json={"error": {"code": "invalid_request", "message": "Invalid", "details": details, "docs": "https://d/e"}},
            headers={"x-ratelimit-remaining": "0"},
        ),
    )
    with pytest.raises(sdk.BadRequestError) as raised:
        api.workspace.get()
    assert raised.value.details == details and raised.value.docs == "https://d/e"
    assert raised.value.headers["x-ratelimit-remaining"] == "0"
    assert str(raised.value) == "Invalid\n  url: Must be https"


def no_wait(monkeypatch, sdk):
    runtime = runtime_module(sdk, "_runtime")
    waits = []
    monkeypatch.setattr(runtime.time, "sleep", waits.append)
    return runtime, waits


def test_a_network_failure_raises_api_connection_error_after_the_retries(sdk, names, monkeypatch):
    no_wait(monkeypatch, sdk)
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ConnectError("refused", request=request)

    api, _ = client(sdk, names, handler)
    with pytest.raises(sdk.APIConnectionError) as raised:
        api.workspace.get()
    assert not isinstance(raised.value, sdk.APITimeoutError)
    assert isinstance(raised.value.__cause__, httpx.ConnectError)
    assert len(calls) == 3


def test_a_timeout_raises_api_timeout_error(sdk, names, monkeypatch):
    no_wait(monkeypatch, sdk)

    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    api, _ = client(sdk, names, handler, max_retries=1)
    with pytest.raises(sdk.APITimeoutError) as raised:
        api.workspace.get()
    assert isinstance(raised.value, sdk.APIConnectionError)
    assert isinstance(raised.value.__cause__, httpx.ReadTimeout)


# ── Retries ──────────────────────────────────────────────────────────────


def test_every_write_gets_a_key_and_retries_reuse_it(sdk, names, monkeypatch):
    no_wait(monkeypatch, sdk)
    answers = [httpx.Response(503), httpx.Response(201, json=ENDPOINT)]
    api, seen = client(sdk, names, lambda r: answers.pop(0))
    api.workspace.webhook_endpoints.create(url="https://example.com/h")
    keys = [r.headers.get("idempotency-key") for r in seen]
    assert len(keys) == 2 and len(keys[0]) == 36 and keys[1] == keys[0]
    assert seen[1].content == seen[0].content


def test_a_timeout_is_retried_with_the_same_key(sdk, names, monkeypatch):
    no_wait(monkeypatch, sdk)
    calls = []

    def handler(request):
        calls.append(request.headers["idempotency-key"])
        if len(calls) == 1:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(201, json={})

    api, _ = client(sdk, names, handler)
    api.workspace.webhook_endpoints.create(url="https://example.com/h", idempotency_key="mine")
    assert calls == ["mine", "mine"]


def test_no_retry_for_a_reused_key_a_4xx_or_max_retries_0(sdk, names, monkeypatch):
    no_wait(monkeypatch, sdk)
    reused = httpx.Response(409, json={"error": {"code": "idempotency_key_reused"}})
    api, seen = client(sdk, names, lambda r: reused)
    with pytest.raises(sdk.ConflictError):
        api.workspace.webhook_endpoints.create(url="https://example.com/h")
    assert len(seen) == 1
    api, seen = client(sdk, names, lambda r: httpx.Response(503), max_retries=0)
    with pytest.raises(sdk.InternalServerError):
        api.workspace.get()
    assert len(seen) == 1


def test_retry_after_is_honoured_up_to_60_seconds(sdk, names, monkeypatch):
    _, waits = no_wait(monkeypatch, sdk)
    answers = [httpx.Response(429, headers={"retry-after": "45"}), httpx.Response(200, json={})]
    api, seen = client(sdk, names, lambda r: answers.pop(0))
    api.workspace.get()
    assert waits == [45.0] and len(seen) == 2
    api, seen = client(sdk, names, lambda r: httpx.Response(429, headers={"retry-after": "61"}))
    with pytest.raises(sdk.RateLimitError) as raised:
        api.workspace.get()
    assert raised.value.headers["retry-after"] == "61" and len(seen) == 1


def test_the_async_client_retries_too(sdk, names, monkeypatch):
    runtime, _ = no_wait(monkeypatch, sdk)

    async def instant(_s):
        return None

    monkeypatch.setattr(runtime.asyncio, "sleep", instant)
    answers = [httpx.Response(502), httpx.Response(201, json={})]
    api, seen = async_client(sdk, names, lambda r: answers.pop(0))
    asyncio.run(api.workspace.webhook_endpoints.create(url="https://example.com/h"))
    assert len(seen) == 2 and seen[0].headers["idempotency-key"] == seen[1].headers["idempotency-key"]


# ── Paging ───────────────────────────────────────────────────────────────


def two_pages(request):
    if request.url.params.get("cursor") == "c2":
        return httpx.Response(200, json={"items": [{**MEMBER, "id": "mem_2"}], "nextCursor": None})
    return httpx.Response(200, json={"items": [MEMBER], "nextCursor": "c2"})


def test_a_list_gives_its_first_page_and_iterates_every_item(sdk, names):
    api, seen = client(sdk, names, two_pages)
    page = api.workspace.members.list(limit=1)
    assert isinstance(page, sdk.SyncPage)
    assert [m.id for m in page.items] == ["mem_1"] and page.next_cursor == "c2"
    assert page.has_next_page()
    last = page.get_next_page()
    assert [m.id for m in last.items] == ["mem_2"] and not last.has_next_page()
    with pytest.raises(RuntimeError):
        last.get_next_page()
    assert [m.id for m in api.workspace.members.list(limit=1)] == ["mem_1", "mem_2"]
    assert seen[-1].url.params["cursor"] == "c2" and seen[-1].url.params["limit"] == "1"
    assert page.to_dict() == {"items": [MEMBER], "nextCursor": "c2"}


def test_an_async_list_awaits_to_its_first_page_or_iterates_every_item(sdk, names):
    api, _ = async_client(sdk, names, two_pages)

    async def main():
        page = await api.workspace.members.list()
        assert isinstance(page, sdk.AsyncPage) and [m.id for m in page.items] == ["mem_1"]
        nxt = await page.get_next_page()
        assert [m.id for m in nxt.items] == ["mem_2"]
        return [m.id async for m in api.workspace.members.list()]

    assert asyncio.run(main()) == ["mem_1", "mem_2"]


# ── Client set-up ────────────────────────────────────────────────────────


def sent(cls, **kwargs):
    transport, seen = fake(lambda r: httpx.Response(200, json={"id": "ws_1", "name": "Acme"}))
    cls(http_client=httpx.Client(transport=transport), **kwargs).workspace.get()
    return seen[0]


def test_sends_api_key_or_token_as_the_bearer_credential(sdk, names):
    cls = getattr(sdk, names["client"])
    assert sent(cls, api_key="sk_key").headers["authorization"] == "Bearer sk_key"
    # token takes an API key too, as it did before api_key existed.
    assert sent(cls, token="sk_key").headers["authorization"] == "Bearer sk_key"
    assert sent(cls, token="pat_x").headers["authorization"] == "Bearer pat_x"
    with pytest.raises(ValueError, match="pass api_key or token, not both"):
        cls(api_key="sk_a", token="pat_b")


def test_reads_api_key_then_token_from_the_environment(sdk, names, monkeypatch):
    cls = getattr(sdk, names["client"])
    token_env = names["key_env"].removesuffix("_API_KEY") + "_TOKEN"
    monkeypatch.setenv(names["key_env"], "sk_from_env")
    monkeypatch.setenv(token_env, "pat_from_env")
    assert sent(cls).headers["authorization"] == "Bearer sk_from_env"
    # A credential you pass wins over either variable.
    assert sent(cls, token="pat_passed").headers["authorization"] == "Bearer pat_passed"
    monkeypatch.delenv(names["key_env"])
    assert sent(cls).headers["authorization"] == "Bearer pat_from_env"
    monkeypatch.delenv(token_env)
    with pytest.raises(ValueError, match=names["key_env"]):
        cls()


def test_base_url_from_the_argument_or_the_environment(sdk, names, monkeypatch):
    cls = getattr(sdk, names["client"])
    base_env = names["key_env"].removesuffix("_API_KEY") + "_BASE_URL"
    assert str(sent(cls, token="t", base_url="http://localhost:8787/").url) == "http://localhost:8787/v1/workspace"
    monkeypatch.setenv(base_env, "https://api.example.test")
    assert str(sent(cls, token="t").url) == "https://api.example.test/v1/workspace"
    assert cls(token="t").base_url == "https://api.example.test"


def test_default_headers_extra_headers_and_with_options(sdk, names):
    api, seen = client(sdk, names, lambda r: httpx.Response(200, json={}), default_headers={"X-Team": "a"})
    api.workspace.get()
    api.workspace.get(extra_headers={"x-team": "b", "x-trace": "1"})
    api.with_options(default_headers={"x-more": "m"}).workspace.get()
    api.workspace.get()
    assert [r.headers.get("x-team") for r in seen] == ["a", "b", "a", "a"]
    assert seen[1].headers["x-trace"] == "1" and seen[2].headers["x-more"] == "m"
    assert "x-more" not in seen[3].headers


def test_a_token_names_its_workspace_and_environment(sdk, names):
    api, seen = client(sdk, names, lambda r: httpx.Response(200, json={}), workspace="acme", environment="live")
    api.workspace.get()
    assert seen[0].headers["x-workspace"] == "acme"
    assert seen[0].headers["x-environment"] == "live"


def test_the_async_client_does_the_same_and_closes(sdk, names):
    api, seen = async_client(sdk, names, lambda r: httpx.Response(200, json={"id": "ws_1", "name": "Acme"}))

    async def main():
        async with api:
            workspace = await api.workspace.get()
        await api.close()  # closing twice is fine
        await api.aclose()
        return workspace

    assert asyncio.run(main()).name == "Acme"
    assert seen[0].url.path == "/v1/workspace"


def test_with_gives_the_product_client(sdk, names):
    cls = getattr(sdk, names["client"])
    transport, _ = fake(lambda r: httpx.Response(200, json={}))
    with cls(token="sk_test", http_client=httpx.Client(transport=transport)) as api:
        assert isinstance(api, cls)
        api.workspace.get()


def test_raw_bytes_go_as_they_are_and_come_back_as_bytes(sdk, names):
    api, seen = client(
        sdk,
        names,
        lambda r: httpx.Response(201, json={"id": "file_1"})
        if r.method == "POST"
        else httpx.Response(200, content=b"id\n2\n"),
    )
    upload = {
        "method": "post",
        "path": "/files",
        "query": ["name"],
        "body": False,
        "names": {},
        "raw_body": "application/octet-stream",
    }
    # A helper's _call without cast_to gets the JSON as it came.
    assert api._call(upload, {}, {"name": "a.csv"}, content=b"id\n1\n", content_type="text/csv") == {"id": "file_1"}
    assert seen[0].content == b"id\n1\n"
    assert seen[0].headers["content-type"] == "text/csv"
    assert seen[0].url.params["name"] == "a.csv"

    download = {
        "method": "get",
        "path": "/files/{fileId}/content",
        "query": [],
        "body": False,
        "names": {},
        "raw_response": True,
    }
    assert api._call(download, {"fileId": "file_1"}, {}) == b"id\n2\n"


def test_timeout_limits_each_attempt_per_client_per_call_or_with_options(sdk, names):
    api, seen = client(sdk, names, lambda r: httpx.Response(200, json={}), timeout=10)
    api.workspace.get()
    api.workspace.get(timeout=2)
    api.with_options(timeout=3).workspace.get()
    assert [r.extensions["timeout"]["read"] for r in seen] == [10, 2, 3]


def test_with_options_leaves_the_client_as_it_was(sdk, names, monkeypatch):
    no_wait(monkeypatch, sdk)
    api, seen = client(sdk, names, lambda r: httpx.Response(503))
    once = api.with_options(max_retries=0)
    assert isinstance(once, type(api)) and once.workspace._client is once
    with pytest.raises(sdk.InternalServerError):
        once.workspace.get()
    assert len(seen) == 1
    with pytest.raises(sdk.InternalServerError):
        api.workspace.get()
    assert len(seen) == 4


def test_cancelling_an_async_call_stops_it_while_it_waits(sdk, names):
    api, seen = async_client(sdk, names, lambda r: httpx.Response(503, headers={"retry-after": "10"}))

    async def main():
        task = asyncio.create_task(api.workspace.get())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(asyncio.wait_for(main(), 2))
    assert len(seen) == 1
