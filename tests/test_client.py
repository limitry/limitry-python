"""The runtime and the generated methods, against a fake API (httpx's mock transport)."""

import asyncio
import json

import httpx
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


def test_sends_the_key_the_body_and_an_idempotency_key(sdk, names):
    api, seen = client(
        sdk,
        names,
        lambda r: httpx.Response(201, json={"endpoint": {"id": "we_1"}, "secret": "whsec_x"}),
    )
    created = api.workspace.webhook_endpoints.create(
        url="https://example.com/h", request_options={"idempotency_key": "k1"}
    )
    assert created["secret"] == "whsec_x"
    request = seen[0]
    assert request.method == "POST"
    assert request.url.path == "/v1/workspace/webhook-endpoints"
    assert request.headers["authorization"] == "Bearer sk_test"
    assert request.headers["idempotency-key"] == "k1"
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


def test_a_failure_raises_api_error_with_the_envelope(sdk, names):
    api, _ = client(
        sdk,
        names,
        lambda r: httpx.Response(
            404, json={"error": {"code": "not_found", "message": "No such endpoint", "requestId": "req_1"}}
        ),
    )
    with pytest.raises(sdk.ApiError) as raised:
        api.workspace.webhook_endpoints.get("we_missing")
    assert (raised.value.status, raised.value.code, raised.value.request_id) == (404, "not_found", "req_1")


def test_reads_the_key_from_the_environment(sdk, names, monkeypatch):
    cls = getattr(sdk, names["client"])
    monkeypatch.setenv(names["key_env"], "sk_from_env")
    transport, seen = fake(lambda r: httpx.Response(200, json={"id": "w1", "name": "Acme"}))
    cls(http_client=httpx.Client(transport=transport)).workspace.get()
    assert seen[0].headers["authorization"] == "Bearer sk_from_env"
    monkeypatch.delenv(names["key_env"])
    with pytest.raises(ValueError, match=names["key_env"]):
        cls()


def test_a_token_names_its_workspace_and_environment(sdk, names):
    api, seen = client(
        sdk, names, lambda r: httpx.Response(200, json={}), workspace="acme", environment="live"
    )
    api.workspace.get()
    assert seen[0].headers["x-workspace"] == "acme"
    assert seen[0].headers["x-environment"] == "live"


def test_the_async_client_does_the_same(sdk, names):
    cls = getattr(sdk, "Async" + names["client"])
    transport, seen = fake(lambda r: httpx.Response(200, json={"id": "w1", "name": "Acme"}))

    async def run():
        async with cls(token="sk_test", http_client=httpx.AsyncClient(transport=transport)) as api:
            return await api.workspace.get()

    assert asyncio.run(run())["name"] == "Acme"
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
    assert api._call(upload, {}, {"name": "a.csv"}, None, b"id\n1\n", "text/csv") == {"id": "file_1"}
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


def no_wait(monkeypatch, sdk):
    runtime = __import__(sdk.__name__ + "._runtime", fromlist=["_runtime"])
    monkeypatch.setattr(runtime.time, "sleep", lambda _s: None)
    return runtime


def test_every_write_gets_a_key_and_retries_reuse_it(sdk, names, monkeypatch):
    no_wait(monkeypatch, sdk)
    answers = [httpx.Response(503), httpx.Response(201, json={"secret": "whsec_x"})]
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
    api.workspace.webhook_endpoints.create(
        url="https://example.com/h", request_options={"idempotency_key": "mine"}
    )
    assert calls == ["mine", "mine"]


def test_no_retry_for_a_reused_key_a_4xx_or_max_retries_0(sdk, names, monkeypatch):
    no_wait(monkeypatch, sdk)
    reused = httpx.Response(409, json={"error": {"code": "idempotency_key_reused"}})
    api, seen = client(sdk, names, lambda r: reused)
    with pytest.raises(sdk.ApiError):
        api.workspace.webhook_endpoints.create(url="https://example.com/h")
    assert len(seen) == 1
    api, seen = client(sdk, names, lambda r: httpx.Response(503), max_retries=0)
    with pytest.raises(sdk.ApiError):
        api.workspace.get()
    assert len(seen) == 1


def test_a_long_retry_after_is_handed_back(sdk, names, monkeypatch):
    no_wait(monkeypatch, sdk)
    api, seen = client(
        sdk, names, lambda r: httpx.Response(429, headers={"retry-after": "120"})
    )
    with pytest.raises(sdk.ApiError) as raised:
        api.workspace.get()
    assert raised.value.status == 429 and len(seen) == 1


def test_the_async_client_retries_too(sdk, names, monkeypatch):
    runtime = no_wait(monkeypatch, sdk)

    async def instant(_s):
        return None

    monkeypatch.setattr(runtime.asyncio, "sleep", instant)
    answers = [httpx.Response(502), httpx.Response(201, json={})]
    seen = []

    def respond(request):
        seen.append(request.headers["idempotency-key"])
        return answers.pop(0)

    cls = getattr(sdk, "Async" + names["client"])
    api = cls(token="sk_test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    asyncio.run(api.workspace.webhook_endpoints.create(url="https://example.com/h"))
    assert len(seen) == 2 and seen[0] == seen[1]
