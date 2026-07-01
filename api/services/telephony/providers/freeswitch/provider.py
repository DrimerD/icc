"""FreeSWITCH implementation of the TelephonyProvider interface.

Inbound-only. FreeSWITCH terminates SIP/RTP and streams call audio to Dograh
over a WebSocket (``mod_audio_fork`` / ``mod_audio_stream``). The integration
has two legs:

1. **HTTP webhook** — on an inbound call, the FreeSWITCH dialplan POSTs to
   ``/api/v1/telephony/inbound/run`` with the called/calling numbers and an
   HMAC signature. Dograh matches the workflow, creates a run and returns the
   WebSocket URL as JSON.
2. **WebSocket** — FreeSWITCH connects ``audio_fork`` to that URL and streams
   L16 audio; :class:`FreeswitchFrameSerializer` bridges it into the pipeline.

Dograh never receives SIP; outbound origination is not supported here.
"""

import hashlib
import hmac
import json
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from loguru import logger

from api.db import db_client
from api.enums import TelephonyCallStatus, WorkflowRunMode
from api.services.telephony.base import (
    CallInitiationResult,
    NormalizedInboundData,
    TelephonyProvider,
)

if TYPE_CHECKING:
    from fastapi import WebSocket


class FreeswitchProvider(TelephonyProvider):
    """FreeSWITCH (audio-fork) implementation of TelephonyProvider."""

    PROVIDER_NAME = WorkflowRunMode.FREESWITCH.value
    WEBHOOK_ENDPOINT = "inbound/run"

    def __init__(self, config: Dict[str, Any]):
        """Initialize from the stored credentials dict.

        Args:
            config: Dictionary containing:
                - account_id: identifier of the FreeSWITCH instance
                - shared_secret: HMAC secret for inbound webhook signatures
                - from_numbers: DIDs/extensions handled by this instance
        """
        self.account_id = config.get("account_id", "")
        self.shared_secret = config.get("shared_secret", "")
        self.from_numbers = config.get("from_numbers", [])
        if isinstance(self.from_numbers, str):
            self.from_numbers = [self.from_numbers]

    # ---------- config ----------

    def validate_config(self) -> bool:
        """A FreeSWITCH config needs an account id and a shared secret."""
        return bool(self.account_id and self.shared_secret)

    async def get_available_phone_numbers(self) -> List[str]:
        """Return the DIDs/extensions configured for this instance."""
        return self.from_numbers

    # ---------- outbound (unsupported) ----------

    async def initiate_call(
        self,
        to_number: str,
        webhook_url: str,
        workflow_run_id: Optional[int] = None,
        from_number: Optional[str] = None,
        **kwargs: Any,
    ) -> CallInitiationResult:
        """Outbound origination is not supported by the FreeSWITCH provider."""
        raise NotImplementedError(
            "FreeSWITCH provider is inbound-only; outbound calls are not supported"
        )

    async def get_call_status(self, call_id: str) -> Dict[str, Any]:
        """Call status is owned by FreeSWITCH, not queried via Dograh."""
        return {"call_id": call_id, "status": "unknown"}

    async def get_call_cost(self, call_id: str) -> Dict[str, Any]:
        """FreeSWITCH does not report call cost to Dograh."""
        return {
            "cost_usd": 0.0,
            "duration": 0,
            "status": "unknown",
            "error": "FreeSWITCH does not support cost retrieval",
        }

    async def verify_webhook_signature(
        self, url: str, params: Dict[str, Any], signature: str
    ) -> bool:
        """Outbound-style webhook verification is unused for FreeSWITCH."""
        return True

    async def get_webhook_response(
        self, workflow_id: int, user_id: int, workflow_run_id: int
    ) -> str:
        """Unused — inbound responses are produced by ``start_inbound_stream``."""
        return ""

    def parse_status_callback(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Best-effort mapping of an optional FreeSWITCH status callback."""
        status_map = {
            "ringing": TelephonyCallStatus.RINGING,
            "answered": TelephonyCallStatus.ANSWERED,
            "in-progress": TelephonyCallStatus.ANSWERED,
            "completed": TelephonyCallStatus.COMPLETED,
            "hangup": TelephonyCallStatus.COMPLETED,
            "failed": TelephonyCallStatus.FAILED,
            "busy": TelephonyCallStatus.BUSY,
            "no-answer": TelephonyCallStatus.NO_ANSWER,
        }
        raw_status = str(data.get("status", "")).lower()
        return {
            "call_id": data.get("call_id") or data.get("CallSid", ""),
            "status": status_map.get(raw_status, raw_status),
            "from_number": data.get("from") or data.get("From"),
            "to_number": data.get("to") or data.get("To"),
            "direction": data.get("direction"),
            "duration": data.get("duration"),
            "extra": data,
        }

    # ---------- inbound ----------

    @classmethod
    def can_handle_webhook(
        cls, webhook_data: Dict[str, Any], headers: Dict[str, str]
    ) -> bool:
        """Detect a FreeSWITCH inbound webhook.

        The dialplan stamps ``X-Dograh-Provider: freeswitch`` on the request.
        We also accept an explicit ``provider`` field in the body.
        """
        provider_header = headers.get("x-dograh-provider", "").lower()
        if provider_header == cls.PROVIDER_NAME:
            return True
        return webhook_data.get("provider") == cls.PROVIDER_NAME

    @staticmethod
    def parse_inbound_webhook(webhook_data: Dict[str, Any]) -> NormalizedInboundData:
        """Normalize the FreeSWITCH inbound webhook payload.

        Accepts both Twilio-style keys (``To``/``From``/``CallSid``) and plain
        lowercase keys (``to``/``from``/``call_id``) for convenience.
        """
        to_number = webhook_data.get("To") or webhook_data.get("to") or ""
        from_number = webhook_data.get("From") or webhook_data.get("from") or ""
        call_id = (
            webhook_data.get("CallSid")
            or webhook_data.get("call_id")
            or webhook_data.get("uuid")
            or ""
        )
        account_id = (
            webhook_data.get("account_id") or webhook_data.get("AccountSid") or ""
        )

        return NormalizedInboundData(
            provider=FreeswitchProvider.PROVIDER_NAME,
            call_id=call_id,
            from_number=from_number,
            to_number=to_number,
            direction="inbound",
            call_status=webhook_data.get("CallStatus", "in-progress"),
            account_id=account_id,
            raw_data=webhook_data,
        )

    @staticmethod
    def validate_account_id(config_data: dict, webhook_account_id: str) -> bool:
        """Match the webhook's account id against the stored configuration."""
        if not webhook_account_id:
            return False
        return config_data.get("account_id") == webhook_account_id

    async def verify_inbound_signature(
        self,
        url: str,
        webhook_data: Dict[str, Any],
        headers: Dict[str, str],
        body: str = "",
    ) -> bool:
        """Verify the authenticity of an inbound webhook.

        Two schemes are supported (use whichever your gateway can produce):

        * **HMAC** — ``X-Dograh-Signature`` = hex HMAC-SHA256 of the raw
          request body keyed by ``shared_secret`` (stronger; use when your
          gateway can compute HMAC).
        * **Token** — ``X-Dograh-Token`` = the ``shared_secret`` itself,
          compared in constant time (simplest for a FreeSWITCH Lua dialplan
          that has no crypto library). Safe over TLS/``wss``.

        If a ``shared_secret`` is configured, a valid signature or token is
        required (missing/wrong → reject). If no secret is configured,
        verification is skipped (return True).
        """
        if not self.shared_secret:
            logger.warning(
                "FreeSWITCH provider has no shared_secret configured; skipping "
                "inbound signature verification"
            )
            return True

        # Scheme 1: HMAC over the raw body.
        provided_sig = headers.get("x-dograh-signature", "")
        if provided_sig:
            expected = hmac.new(
                self.shared_secret.encode("utf-8"),
                body.encode("utf-8") if isinstance(body, str) else (body or b""),
                hashlib.sha256,
            ).hexdigest()
            if hmac.compare_digest(expected, provided_sig):
                return True
            logger.warning("FreeSWITCH inbound HMAC signature mismatch")
            return False

        # Scheme 2: shared-secret token header.
        provided_token = headers.get("x-dograh-token", "")
        if provided_token:
            if hmac.compare_digest(self.shared_secret, provided_token):
                return True
            logger.warning("FreeSWITCH inbound token mismatch")
            return False

        logger.warning(
            "FreeSWITCH inbound webhook missing X-Dograh-Signature / X-Dograh-Token"
        )
        return False

    async def start_inbound_stream(
        self,
        *,
        websocket_url: str,
        workflow_run_id: int,
        normalized_data: NormalizedInboundData,
        backend_endpoint: str,
    ):
        """Tell FreeSWITCH where to connect its audio-fork WebSocket.

        Returns a small JSON body. The dialplan reads ``websocket_url`` and
        feeds it to the ``audio_fork`` application.
        """
        from fastapi.responses import JSONResponse

        return JSONResponse(
            content={
                "websocket_url": websocket_url,
                "workflow_run_id": workflow_run_id,
                "call_id": normalized_data.call_id,
            }
        )

    # ---------- websocket ----------

    async def handle_websocket(
        self,
        websocket: "WebSocket",
        workflow_id: int,
        user_id: int,
        workflow_run_id: int,
    ) -> None:
        """Run the pipeline over the FreeSWITCH audio-fork WebSocket.

        Like Asterisk ``chan_websocket``, ``mod_audio_fork`` starts streaming
        immediately (after an optional initial JSON metadata text frame, which
        the serializer ignores) — there is no Twilio-style connected/start
        handshake to await here.
        """
        from api.services.pipecat.run_pipeline import run_pipeline_telephony

        workflow_run = await db_client.get_workflow_run(workflow_run_id, user_id)
        call_id = ""
        if workflow_run and workflow_run.gathered_context:
            call_id = workflow_run.gathered_context.get("call_id", "")

        logger.info(
            f"[FreeSWITCH] Starting pipeline for workflow_run {workflow_run_id}, "
            f"call_id={call_id}"
        )

        await run_pipeline_telephony(
            websocket,
            provider_name=self.PROVIDER_NAME,
            workflow_id=workflow_id,
            workflow_run_id=workflow_run_id,
            user_id=user_id,
            call_id=call_id,
            transport_kwargs={"call_id": call_id},
        )

    # ---------- errors ----------

    @staticmethod
    def generate_error_response(error_type: str, message: str) -> tuple:
        """Generate a JSON error response for the dialplan to act on."""
        from fastapi.responses import JSONResponse

        return JSONResponse(
            content={"error": error_type, "message": message}, status_code=400
        )

    @staticmethod
    def generate_validation_error_response(error_type) -> tuple:
        """JSON error response for inbound validation failures."""
        from fastapi.responses import JSONResponse

        from api.errors.telephony_errors import TELEPHONY_ERROR_MESSAGES, TelephonyError

        message = TELEPHONY_ERROR_MESSAGES.get(
            error_type, TELEPHONY_ERROR_MESSAGES[TelephonyError.GENERAL_AUTH_FAILED]
        )
        return JSONResponse(
            content={"error": str(error_type), "message": message}, status_code=400
        )

    # ---------- transfers (unsupported) ----------

    def supports_transfers(self) -> bool:
        """Call transfers are not implemented for the FreeSWITCH provider."""
        return False

    async def transfer_call(
        self,
        destination: str,
        transfer_id: str,
        conference_name: str,
        timeout: int = 30,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Not supported."""
        raise NotImplementedError("FreeSWITCH provider does not support call transfers")
