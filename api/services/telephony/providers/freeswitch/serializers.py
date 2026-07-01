"""FreeSWITCH ``mod_audio_fork`` / ``mod_audio_stream`` WebSocket serializer.

Wire protocol (as implemented by drachtio ``mod_audio_fork`` and amigniter
``mod_audio_stream``):

* FreeSWITCH -> Dograh:
    - optional initial **text** frame: JSON metadata passed to the
      ``audio_fork`` app (ignored here, returned as ``None``)
    - **binary** frames: raw L16 (16-bit signed little-endian PCM), mono, at
      the sampling rate requested in the ``audio_fork`` command
* Dograh -> FreeSWITCH (to play audio back to the caller):
    - **text** frame:
      ``{"type": "streamAudio", "data": {"audioDataType": "raw",
         "sampleRate": <rate>, "audioData": "<base64 PCM>"}}``

This mirrors ``VonageFrameSerializer`` on the input side (binary L16) but
differs on the output side, where FreeSWITCH expects the ``streamAudio`` JSON
envelope rather than raw binary.
"""

import base64
import json

from loguru import logger

from pipecat.audio.utils import create_stream_resampler
from pipecat.frames.frames import (
    AudioRawFrame,
    CancelFrame,
    EndFrame,
    Frame,
    InputAudioRawFrame,
    OutputTransportMessageFrame,
    OutputTransportMessageUrgentFrame,
    StartFrame,
)
from pipecat.serializers.base_serializer import FrameSerializer


class FreeswitchFrameSerializer(FrameSerializer):
    """Serializer for the FreeSWITCH audio-fork WebSocket protocol."""

    class InputParams(FrameSerializer.InputParams):
        """Configuration parameters.

        Parameters:
            freeswitch_sample_rate: Wire-format rate of the L16 stream from
                FreeSWITCH. Must match the ``sampling-rate`` argument of the
                ``audio_fork`` command (8000 or 16000). Defaults to 8000.
            sample_rate: Optional override for the pipeline input rate.
        """

        freeswitch_sample_rate: int = 8000
        sample_rate: int | None = None

    def __init__(
        self,
        call_id: str,
        params: InputParams | None = None,
    ):
        """Initialize the serializer.

        Args:
            call_id: FreeSWITCH channel UUID (used for logging/correlation).
            params: Configuration parameters.
        """
        params = params or FreeswitchFrameSerializer.InputParams()
        super().__init__(params)
        self._params: FreeswitchFrameSerializer.InputParams = params

        self._call_id = call_id
        self._fs_sample_rate = self._params.freeswitch_sample_rate
        self._sample_rate = 0  # pipeline input rate, set in setup()

        self._input_resampler = create_stream_resampler()
        self._output_resampler = create_stream_resampler()

    async def setup(self, frame: StartFrame):
        """Capture the pipeline input sample rate from the StartFrame."""
        self._sample_rate = self._params.sample_rate or frame.audio_in_sample_rate
        logger.info(
            f"FreeSWITCH serializer ready (call_id={self._call_id}, "
            f"fs_rate={self._fs_sample_rate}, pipeline_rate={self._sample_rate})"
        )

    async def serialize(self, frame: Frame) -> str | bytes | None:
        """Serialize a Pipecat frame to a FreeSWITCH WebSocket message."""
        if isinstance(frame, (EndFrame, CancelFrame)):
            # Hangup is driven by the FreeSWITCH dialplan, not the audio
            # stream. Nothing to send on the WebSocket.
            return None

        if isinstance(frame, AudioRawFrame):
            data = frame.audio

            # Resample pipeline PCM to FreeSWITCH's wire rate (16-bit linear PCM).
            resampled = await self._output_resampler.resample(
                data, frame.sample_rate, self._fs_sample_rate
            )
            if not resampled:
                return None

            return json.dumps(
                {
                    "type": "streamAudio",
                    "data": {
                        "audioDataType": "raw",
                        "sampleRate": self._fs_sample_rate,
                        "audioData": base64.b64encode(resampled).decode("ascii"),
                    },
                }
            )

        if isinstance(
            frame, (OutputTransportMessageFrame, OutputTransportMessageUrgentFrame)
        ):
            if self.should_ignore_frame(frame):
                return None
            return json.dumps(frame.message)

        return None

    async def deserialize(self, data: str | bytes) -> Frame | None:
        """Deserialize a FreeSWITCH WebSocket message into a Pipecat frame."""
        # Binary frame = raw L16 PCM audio from FreeSWITCH.
        if isinstance(data, (bytes, bytearray)):
            resampled = await self._input_resampler.resample(
                bytes(data), self._fs_sample_rate, self._sample_rate
            )
            if not resampled:
                return None
            return InputAudioRawFrame(
                audio=resampled,
                num_channels=1,
                sample_rate=self._sample_rate,
            )

        # Text frame = JSON control/metadata (initial metadata, transcription
        # events, etc.). We don't need any of these to run the pipeline.
        try:
            message = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            logger.debug(f"FreeSWITCH non-JSON text frame ignored: {data!r}")
            return None

        logger.debug(f"FreeSWITCH control message ignored: {message}")
        return None
