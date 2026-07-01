"""FreeSWITCH telephony configuration schemas.

FreeSWITCH acts as the SIP/RTP termination point and streams call audio to
Dograh over a WebSocket via ``mod_audio_fork`` / ``mod_audio_stream``. Dograh
never speaks SIP — it only consumes the audio stream and matches the inbound
call to a workflow.
"""

from typing import List, Literal

from pydantic import BaseModel, Field


class FreeswitchConfigurationRequest(BaseModel):
    """Request schema for FreeSWITCH configuration.

    Inbound-only: ``account_id`` identifies the FreeSWITCH instance (sent on
    each inbound webhook so the right config row is matched), ``shared_secret``
    signs the inbound webhook (HMAC-SHA256 over the raw body).
    """

    provider: Literal["freeswitch"] = Field(default="freeswitch")
    account_id: str = Field(
        ...,
        description="Identifier of your FreeSWITCH instance, sent as X-Dograh-Account on inbound webhooks",
    )
    shared_secret: str = Field(
        ...,
        description="Secret used to HMAC-sign inbound webhooks (X-Dograh-Signature)",
    )
    from_numbers: List[str] = Field(
        default_factory=list,
        description="DIDs / extensions handled by this FreeSWITCH instance",
    )


class FreeswitchConfigurationResponse(BaseModel):
    """Response schema for FreeSWITCH configuration with masked secrets."""

    provider: Literal["freeswitch"] = Field(default="freeswitch")
    account_id: str
    shared_secret: str  # masked when returned
    from_numbers: List[str]
