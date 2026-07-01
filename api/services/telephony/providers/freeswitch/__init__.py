"""FreeSWITCH telephony provider package.

Self-registering: importing this package registers the ``freeswitch``
provider with the telephony registry. Inbound-only — FreeSWITCH terminates
SIP/RTP and streams audio to Dograh over a WebSocket via ``mod_audio_fork``.
"""

from typing import Any, Dict

from api.services.telephony.registry import (
    ProviderSpec,
    ProviderUIField,
    ProviderUIMetadata,
    register,
)

from .config import FreeswitchConfigurationRequest, FreeswitchConfigurationResponse
from .provider import FreeswitchProvider
from .transport import create_transport


def _config_loader(value: Dict[str, Any]) -> Dict[str, Any]:
    """Reshape the stored credentials dict into the constructor shape."""
    return {
        "provider": "freeswitch",
        "account_id": value.get("account_id"),
        "shared_secret": value.get("shared_secret"),
        "from_numbers": value.get("from_numbers", []),
    }


_UI_METADATA = ProviderUIMetadata(
    display_name="FreeSWITCH",
    docs_url="https://docs.dograh.com/integrations/telephony/freeswitch",
    fields=[
        ProviderUIField(
            name="account_id",
            label="Account ID",
            type="text",
            description="Identifier of your FreeSWITCH instance (sent on inbound webhooks)",
        ),
        ProviderUIField(
            name="shared_secret",
            label="Shared Secret",
            type="password",
            sensitive=True,
            description="Secret used to HMAC-sign inbound webhooks",
        ),
        ProviderUIField(
            name="from_numbers",
            label="DIDs / Extensions",
            type="string-array",
            required=False,
        ),
    ],
)


SPEC = ProviderSpec(
    name="freeswitch",
    provider_cls=FreeswitchProvider,
    config_loader=_config_loader,
    transport_factory=create_transport,
    transport_sample_rate=8000,
    config_request_cls=FreeswitchConfigurationRequest,
    config_response_cls=FreeswitchConfigurationResponse,
    ui_metadata=_UI_METADATA,
    account_id_credential_field="account_id",
)


register(SPEC)


__all__ = [
    "SPEC",
    "FreeswitchConfigurationRequest",
    "FreeswitchConfigurationResponse",
    "FreeswitchProvider",
    "create_transport",
]
