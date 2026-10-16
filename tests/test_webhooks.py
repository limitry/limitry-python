"""Webhook verification, against a delivery signed by the platform's own
signer (packages/jobs/src/webhooks.ts) — the same check the TypeScript SDK
makes (packages/sdk/src/webhooks.test.ts)."""

import pytest

# Signed by signWebhook(secret, "dlv_1", TIMESTAMP, BODY).
SECRET = "whsec_MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
BODY = '{"id":"evt_1","type":"member.joined","createdAt":"2026-10-11T12:00:00.000Z","data":{"userId":"u_1"}}'
TIMESTAMP = 1791806400
SIGNATURE = "v1,B/VTHw36Mavru9zh0bs5VFKKPaw3DAis5T7VRXmQj+Q="
HEADERS = {
    "webhook-id": "dlv_1",
    "webhook-timestamp": str(TIMESTAMP),
    "webhook-signature": SIGNATURE,
}


def test_returns_the_event_of_a_delivery_we_signed(sdk):
    event = sdk.verify_webhook(BODY, HEADERS, SECRET, now=TIMESTAMP + 10)
    assert event == {
        "id": "evt_1",
        "type": "member.joined",
        "createdAt": "2026-10-11T12:00:00.000Z",
        "data": {"userId": "u_1"},
    }
    # Bytes and any header casing work too.
    upper = {k.title(): v for k, v in HEADERS.items()}
    assert sdk.verify_webhook(BODY.encode(), upper, SECRET, now=TIMESTAMP)["id"] == "evt_1"


def test_is_on_both_clients(sdk, names, monkeypatch):
    for cls in (getattr(sdk, names["client"]), getattr(sdk, "Async" + names["client"])):
        client = cls(token="sk_test")
        monkeypatch.setattr("time.time", lambda: TIMESTAMP)
        assert client.webhooks.verify(BODY, HEADERS, SECRET)["type"] == "member.joined"


def test_refuses_altered_bodies_other_secrets_old_deliveries_and_missing_headers(sdk):
    with pytest.raises(sdk.WebhookVerificationError, match="signature"):
        sdk.verify_webhook(BODY.replace("u_1", "u_2"), HEADERS, SECRET, now=TIMESTAMP)
    other = "whsec_" + "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVphYmNkZWY="
    with pytest.raises(sdk.WebhookVerificationError, match="signature"):
        sdk.verify_webhook(BODY, HEADERS, other, now=TIMESTAMP)
    with pytest.raises(sdk.WebhookVerificationError, match="too old"):
        sdk.verify_webhook(BODY, HEADERS, SECRET, now=TIMESTAMP + 600)
    with pytest.raises(sdk.WebhookVerificationError, match="Missing"):
        sdk.verify_webhook(BODY, {}, SECRET, now=TIMESTAMP)


def test_accepts_any_of_several_signatures(sdk):
    headers = {**HEADERS, "webhook-signature": f"v1,AAAA {SIGNATURE}"}
    assert sdk.verify_webhook(BODY, headers, SECRET, now=TIMESTAMP)["id"] == "evt_1"
