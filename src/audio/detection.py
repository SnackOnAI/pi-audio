"""Sound activity and speech classification over brokered PCM frames."""

from __future__ import annotations

import asyncio
import logging
import math
import struct
from abc import ABC, abstractmethod
from collections import deque
from contextlib import suppress
from dataclasses import dataclass

import webrtcvad  # type: ignore[import-untyped]

from ..models import ActivityConfig, AudioConfig, RecordingConfig, VadConfig
from .broker import AudioFrameBroker, AudioFrameSubscription
from .models import AudioFrame
from .recording import AudioRecorder


class AudioDetectionError(RuntimeError):
    """Raised when PCM cannot be classified safely."""


@dataclass(frozen=True, slots=True)
class ActivityDecision:
    sequence: int
    active: bool
    level_dbfs: float


@dataclass(frozen=True, slots=True)
class VoiceDecision:
    sequence: int
    speech: bool


class AudioActivityDetector(ABC):
    @abstractmethod
    def classify(self, frame: AudioFrame) -> ActivityDecision:
        """Classify any audible activity, including non-speech sound."""


class VoiceActivityDetector(ABC):
    @abstractmethod
    def classify(self, frame: AudioFrame) -> VoiceDecision:
        """Classify speech independently from recording decisions."""


class RmsAudioActivityDetector(AudioActivityDetector):
    """Detect sound using the RMS level of signed 16-bit mono PCM."""

    def __init__(self, threshold_dbfs: float) -> None:
        if not -96.0 <= threshold_dbfs <= 0.0:
            raise ValueError("threshold_dbfs must be between -96.0 and 0.0")
        self._threshold_dbfs = threshold_dbfs

    def classify(self, frame: AudioFrame) -> ActivityDecision:
        _validate_detection_frame(frame)
        sample_count = frame.frame_count
        sum_squares = sum(
            sample * sample for (sample,) in struct.iter_unpack("<h", frame.data)
        )
        rms = math.sqrt(sum_squares / sample_count)
        level_dbfs = 20.0 * math.log10(rms / 32_768.0) if rms else -math.inf
        return ActivityDecision(
            sequence=frame.sequence,
            active=level_dbfs >= self._threshold_dbfs,
            level_dbfs=level_dbfs,
        )


class WebRtcVoiceActivityDetector(VoiceActivityDetector):
    """Classify speech with the lightweight native WebRTC VAD."""

    def __init__(self, audio_config: AudioConfig, aggressiveness: int) -> None:
        if aggressiveness not in (0, 1, 2, 3):
            raise ValueError("aggressiveness must be between 0 and 3")
        if (
            audio_config.sample_rate != 16_000
            or audio_config.channels != 1
            or audio_config.sample_width_bytes != 2
            or audio_config.chunk_duration_ms not in (10, 20, 30)
        ):
            raise ValueError(
                "WebRTC VAD requires 16 kHz, mono, signed 16-bit PCM in "
                "10, 20, or 30 ms frames"
            )
        self._sample_rate = audio_config.sample_rate
        self._vad = webrtcvad.Vad(aggressiveness)

    def classify(self, frame: AudioFrame) -> VoiceDecision:
        _validate_detection_frame(frame)
        try:
            speech = self._vad.is_speech(frame.data, self._sample_rate)
        except webrtcvad.Error as exc:
            raise AudioDetectionError(f"WebRTC VAD rejected frame: {exc}") from exc
        return VoiceDecision(sequence=frame.sequence, speech=speech)


class SoundRecordingService:
    """Record audible events while classifying speech independently."""

    def __init__(
        self,
        broker: AudioFrameBroker,
        activity_detector: AudioActivityDetector,
        recorder: AudioRecorder,
        audio_config: AudioConfig,
        activity_config: ActivityConfig,
        recording_config: RecordingConfig,
        *,
        voice_detector: VoiceActivityDetector | None = None,
        vad_config: VadConfig | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._broker = broker
        self._activity_detector = activity_detector
        self._voice_detector = voice_detector
        self._recorder = recorder
        self._audio_config = audio_config
        self._activity_config = activity_config
        self._recording_config = recording_config
        self._vad_config = vad_config
        self._logger = logger or logging.getLogger("audio_stack.detection")
        self._pre_buffer: deque[AudioFrame] = deque(
            maxlen=max(
                self._frame_count(activity_config.pre_buffer_ms),
                self._frame_count(activity_config.minimum_active_ms),
            )
        )
        self._active_frames = 0
        self._silent_frames = 0
        self._recorded_pcm_frames = 0
        self._speech_frames = 0
        self._speech_active = False
        self._recording = False
        self._subscription: AudioFrameSubscription | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.is_running:
            raise RuntimeError("sound recording service is already running")
        self._subscription = self._broker.subscribe()
        self._task = asyncio.create_task(self._run(), name="sound-activity-recording")

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        task.cancel()
        try:
            with suppress(asyncio.CancelledError):
                await task
        finally:
            if self._subscription is not None:
                self._subscription.close()
                self._subscription = None
            await self._finish_recording("service_stopped")
            self._task = None

    async def wait(self) -> None:
        if self._task is not None:
            await asyncio.shield(self._task)

    async def _run(self) -> None:
        subscription = self._subscription
        if subscription is None:
            raise RuntimeError("sound recording subscription is unavailable")
        async for frame in subscription:
            await self._process_frame(frame)

    async def _process_frame(self, frame: AudioFrame) -> None:
        self._classify_voice(frame)
        activity = self._activity_detector.classify(frame)

        if not self._recording:
            self._pre_buffer.append(frame)
            self._active_frames = self._active_frames + 1 if activity.active else 0
            if self._active_frames >= self._minimum_active_frames:
                await self._start_recording(activity.level_dbfs)
            return

        if self._would_exceed_maximum(frame):
            await self._finish_recording("maximum_duration")
            session = await self._recorder.start()
            self._recording = True
            self._logger.info(
                "Sound event recording continued in a new segment",
                extra={
                    "event": "sound_segment_started",
                    "recording_id": session.recording_id,
                },
            )

        await self._recorder.write_frame(frame)
        self._recorded_pcm_frames += frame.frame_count
        self._silent_frames = 0 if activity.active else self._silent_frames + 1
        if self._silent_frames >= self._ending_silence_frames:
            await self._finish_recording("silence")

    async def _start_recording(self, level_dbfs: float) -> None:
        session = await self._recorder.start()
        self._recording = True
        self._recorded_pcm_frames = 0
        for buffered_frame in self._pre_buffer:
            await self._recorder.write_frame(buffered_frame)
            self._recorded_pcm_frames += buffered_frame.frame_count
        self._pre_buffer.clear()
        self._silent_frames = 0
        self._logger.info(
            "Sound event started",
            extra={
                "event": "sound_started",
                "recording_id": session.recording_id,
                "level_dbfs": level_dbfs,
            },
        )

    async def _finish_recording(self, reason: str) -> None:
        if not self._recording:
            return
        result = await self._recorder.finish()
        self._recording = False
        self._recorded_pcm_frames = 0
        self._active_frames = 0
        self._silent_frames = 0
        self._logger.info(
            "Sound event ended",
            extra={
                "event": "sound_ended",
                "reason": reason,
                "recording_id": result.recording_id if result else None,
            },
        )

    def _classify_voice(self, frame: AudioFrame) -> None:
        if self._voice_detector is None or self._vad_config is None:
            return
        decision = self._voice_detector.classify(frame)
        self._speech_frames = self._speech_frames + 1 if decision.speech else 0
        if not self._speech_active and self._speech_frames >= self._speech_start_frames:
            self._speech_active = True
            self._logger.info(
                "Speech detected",
                extra={"event": "speech_started", "sequence": frame.sequence},
            )
        elif self._speech_active and not decision.speech:
            self._speech_active = False
            self._logger.info(
                "Speech ended",
                extra={"event": "speech_ended", "sequence": frame.sequence},
            )

    def _would_exceed_maximum(self, frame: AudioFrame) -> bool:
        maximum = (
            self._recording_config.maximum_duration_seconds
            * self._audio_config.sample_rate
        )
        return self._recorded_pcm_frames + frame.frame_count > maximum

    def _frame_count(self, milliseconds: int) -> int:
        return math.ceil(milliseconds / self._audio_config.chunk_duration_ms)

    @property
    def _minimum_active_frames(self) -> int:
        return self._frame_count(self._activity_config.minimum_active_ms)

    @property
    def _ending_silence_frames(self) -> int:
        return self._frame_count(
            self._activity_config.silence_timeout_ms
            + self._activity_config.post_buffer_ms
        )

    @property
    def _speech_start_frames(self) -> int:
        assert self._vad_config is not None
        return self._frame_count(self._vad_config.minimum_speech_ms)


def _validate_detection_frame(frame: AudioFrame) -> None:
    if (
        frame.sample_rate != 16_000
        or frame.channels != 1
        or frame.sample_width_bytes != 2
    ):
        raise AudioDetectionError("detection requires 16 kHz, mono, signed 16-bit PCM")
