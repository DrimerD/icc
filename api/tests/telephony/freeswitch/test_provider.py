import hashlib
import hmac
import json

import pytest

from api.services.telephony.providers.freeswitch import (
    SPEC,
    FreeswitchConfigurationRequest,
)
from api.services.telephony.providers.freeswitch.provider import FreeswitchProvider


def _provider(secret: str = "topsecret") -> FreeswitchProvider:
    return FreeswitchProvider(
        {
            "account_id": "fs-prod-1",
            "shared_secret": secret,
            "from_numbers": ["+380441234567"],
        }
    )


def test_validate_config():
    assert _provider().validate_config() is True
    assert FreeswitchProvider({"account_id": "x"}).validate_config() is False
    assert FreeswitchProvider({"shared_secret": "y"}).validate_config() is False


def test_spec_registered():
    assert SPEC.name == "freeswitch"
    assert SPEC.account_id_credential_field == "account_id"
    assert SPEC.transport_sample_rate == 8000
    assert SPEC.provider_cls is FreeswitchProvider


def test_config_request_discriminator():
    req = FreeswitchConfigurationRequest(
        account_id="fs-prod-1", shared_secret="s", from_numbers=["+380441234567"]
    )
    assert req.provider == "freeswitch"


def test_can_handle_webhook_by_header():
    assert (
        FreeswitchProvider.can_handle_webhook({}, {"x-dograh-provider": "freeswitch"})
        is True
    )
    assert (
        FreeswitchProvider.can_handle_webhook({"provider": "freeswitch"}, {}) is True
    )
    assert FreeswitchProvider.can_handle_webhook({}, {}) is False
    assert (
        FreeswitchProvider.can_handle_webhook({}, {"x-dograh-provider": "twilio"})
        is False
    )


def test_parse_inbound_webhook_twilio_style():
    data = {
        "To": "+380441234567",
        "From": "+380501112233",
        "CallSid": "abc-123",
        "account_id": "fs-prod-1",
    }
    nd = FreeswitchProvider.parse_inbound_webhook(data)
    assert nd.provider == "freeswitch"
    assert nd.to_number == "+380441234567"
    assert nd.from_number == "+380501112233"
    assert nd.call_id == "abc-123"
    assert nd.account_id == "fs-prod-1"
    assert nd.direction == "inbound"


def test_parse_inbound_webhook_lowercase_keys():
    data = {"to": "8000", "from": "7000", "uuid": "u-9", "account_id": "fs"}
    nd = FreeswitchProvider.parse_inbound_webhook(data)
    assert nd.to_number == "8000"
    assert nd.from_number == "7000"
    assert nd.call_id == "u-9"


def test_validate_account_id():
    cfg = {"account_id": "fs-prod-1"}
    assert FreeswitchProvider.validate_account_id(cfg, "fs-prod-1") is True
    assert FreeswitchProvider.validate_account_id(cfg, "other") is False
    assert FreeswitchProvider.validate_account_id(cfg, "") is False


@pytest.mark.asyncio
async def test_verify_inbound_signature_token():
    p = _provider("topsecret")
    assert (
        await p.verify_inbound_signature("u", {}, {"x-dograh-token": "topsecret"}, "")
        is True
    )
    assert (
        await p.verify_inbound_signature("u", {}, {"x-dograh-token": "wrong"}, "")
        is False
    )


@pytest.mark.asyncio
async def test_verify_inbound_signature_hmac():
    p = _provider("topsecret")
    body = json.dumps({"To": "+380441234567"})
    sig = hmac.new(b"topsecret", body.encode(), hashlib.sha256).hexdigest()
    assert (
        await p.verify_inbound_signature("u", {}, {"x-dograh-signature": sig}, body)
        is True
    )
    assert (
        await p.verify_inbound_signature(
            "u", {}, {"x-dograh-signature": "deadbeef"}, body
        )
        is False
    )


@pytest.mark.asyncio
async def test_verify_inbound_signature_missing():
    # secret configured but no header → reject
    assert await _provider("s").verify_inbound_signature("u", {}, {}, "") is False
    # no secret configured → skip verification
    p = FreeswitchProvider({"account_id": "a"})
    assert await p.verify_inbound_signature("u", {}, {}, "") is True


@pytest.mark.asyncio
async def test_start_inbound_stream_returns_ws_url():
    from api.services.telephony.base import NormalizedInboundData

    nd = NormalizedInboundData(
        provider="freeswitch",
        call_id="abc-123",
        from_number="+380501112233",
        to_number="+380441234567",
        direction="inbound",
        call_status="in-progress",
        account_id="fs-prod-1",
    )
    resp = await _provider().start_inbound_stream(
        websocket_url="wss://api.dograh.com/api/v1/telephony/ws/1/2/3",
        workflow_run_id=3,
        normalized_data=nd,
        backend_endpoint="https://api.dograh.com",
    )
    payload = json.loads(bytes(resp.body).decode())
    assert payload["websocket_url"].endswith("/ws/1/2/3")
    assert payload["call_id"] == "abc-123"


def test_supports_transfers_false():
    assert _provider().supports_transfers() is False


@pytest.mark.asyncio
async def test_initiate_call_unsupported():
    with pytest.raises(NotImplementedError):
        await _provider().initiate_call("+380441234567", "http://x")
