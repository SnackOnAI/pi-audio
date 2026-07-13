from __future__ import annotations

import logging
import math
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from src.audio.broker import AudioFrameBroker
from src.audio.detection import (
    ActivityDecision,
    AudioActivityDetector,
    RmsAudioActivityDetector,
    SoundRecordingService,
    VoiceActivityDetector,
    VoiceDecision,
    WebRtcVoiceActivityDetector,
)
from src.audio.models import AudioFrame
from src.audio.recording import AudioRecorder, RecordingResult, RecordingSession
from src.models import ActivityConfig, AudioConfig, RecordingConfig, VadConfig
from src.recording_control import set_recording_paused


def audio_config() -> AudioConfig:
    return AudioConfig("test", 16_000, 1, 2, 30, 20)


def activity_config(*, pre_buffer_ms: int = 60) -> ActivityConfig:
    return ActivityConfig(True, -45.0, 60, 30, pre_buffer_ms, 0)


def recording_config() -> RecordingConfig:
    return RecordingConfig(Path("recordings"), "mp3", 64, 0, 10, True)


def vad_config() -> VadConfig:
    return VadConfig(True, "webrtc", 2, 60)


def pcm_frame(sequence: int, amplitude: int) -> AudioFrame:
    sample = int(amplitude).to_bytes(2, "little", signed=True)
    return AudioFrame(sample * 480, sequence, float(sequence), 16_000, 1, 2)


class FixedActivityDetector(AudioActivityDetector):
    def __init__(self, active: bool) -> None:
        self.active = active

    def classify(self, frame: AudioFrame) -> ActivityDecision:
        return ActivityDecision(frame.sequence, self.active, -10.0)


class FixedVoiceDetector(VoiceActivityDetector):
    def __init__(self, speech: bool) -> None:
        self.speech = speech

    def classify(self, frame: AudioFrame) -> VoiceDecision:
        return VoiceDecision(frame.sequence, self.speech)


class FakeRecorder(AudioRecorder):
    def __init__(self) -> None:
        self.sessions = 0
        self.frames: list[AudioFrame] = []
        self.finished = 0
        self.active = False

    async def start(self) -> RecordingSession:
        self.sessions += 1
        self.active = True
        return RecordingSession(
            str(self.sessions),
            datetime.now(timezone.utc),
            Path(f"recording-{self.sessions}.mp3"),
        )

    async def write_frame(self, frame: AudioFrame) -> None:
        self.frames.append(frame)

    async def finish(self) -> RecordingResult | None:
        self.finished += 1
        self.active = False
        return None

    async def abort(self) -> None:
        self.active = False


class AudioDetectionTests(unittest.IsolatedAsyncioTestCase):
    def test_rms_detector_reports_silence_and_sound(self) -> None:
        detector = RmsAudioActivityDetector(-45.0)

        silence = detector.classify(pcm_frame(0, 0))
        sound = detector.classify(pcm_frame(1, 10_000))

        self.assertFalse(silence.active)
        self.assertEqual(silence.level_dbfs, -math.inf)
        self.assertTrue(sound.active)
        self.assertGreater(sound.level_dbfs, -11.0)

    def test_webrtc_detector_accepts_pipeline_format(self) -> None:
        detector = WebRtcVoiceActivityDetector(audio_config(), 2)

        decision = detector.classify(pcm_frame(0, 0))

        self.assertFalse(decision.speech)

    async def test_non_speech_sound_starts_and_finishes_recording(self) -> None:
        recorder = FakeRecorder()
        service = self._service(
            FixedActivityDetector(True), FixedVoiceDetector(False), recorder
        )

        await service._process_frame(pcm_frame(0, 10_000))
        await service._process_frame(pcm_frame(1, 10_000))

        self.assertEqual(recorder.sessions, 1)
        self.assertEqual([frame.sequence for frame in recorder.frames], [0, 1])

        service._activity_detector = FixedActivityDetector(False)
        await service._process_frame(pcm_frame(2, 0))

        self.assertEqual(recorder.finished, 1)

    async def test_speech_classification_does_not_trigger_recording(self) -> None:
        recorder = FakeRecorder()
        service = self._service(
            FixedActivityDetector(False), FixedVoiceDetector(True), recorder
        )

        await service._process_frame(pcm_frame(0, 0))
        await service._process_frame(pcm_frame(1, 0))

        self.assertEqual(recorder.sessions, 0)
        self.assertTrue(service._speech_active)

    async def test_trigger_frames_are_kept_without_a_pre_buffer(self) -> None:
        recorder = FakeRecorder()
        service = self._service(
            FixedActivityDetector(True),
            FixedVoiceDetector(False),
            recorder,
            activity=activity_config(pre_buffer_ms=0),
        )

        await service._process_frame(pcm_frame(0, 10_000))
        await service._process_frame(pcm_frame(1, 10_000))

        self.assertEqual([frame.sequence for frame in recorder.frames], [0, 1])

    async def test_long_sound_is_split_at_maximum_duration(self) -> None:
        recorder = FakeRecorder()
        short_recording = RecordingConfig(Path("recordings"), "mp3", 64, 0, 1, True)
        service = self._service(
            FixedActivityDetector(True),
            FixedVoiceDetector(False),
            recorder,
            recording=short_recording,
        )

        for sequence in range(35):
            await service._process_frame(pcm_frame(sequence, 10_000))

        self.assertEqual(recorder.sessions, 2)
        self.assertEqual(recorder.finished, 1)
        self.assertEqual(len(recorder.frames), 35)

    async def test_operator_pause_closes_recording_and_discards_paused_buffer(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            recording = RecordingConfig(Path(directory), "mp3", 64, 0, 10, True)
            recorder = FakeRecorder()
            service = self._service(
                FixedActivityDetector(True),
                FixedVoiceDetector(False),
                recorder,
                recording=recording,
            )

            await service._process_frame(pcm_frame(0, 10_000))
            await service._process_frame(pcm_frame(1, 10_000))
            set_recording_paused(recording.directory, True)
            await service._process_frame(pcm_frame(2, 10_000))
            await service._process_frame(pcm_frame(3, 10_000))

            self.assertEqual(recorder.sessions, 1)
            self.assertEqual(recorder.finished, 1)
            self.assertEqual([frame.sequence for frame in recorder.frames], [0, 1])

            set_recording_paused(recording.directory, False)
            await service._process_frame(pcm_frame(4, 10_000))
            await service._process_frame(pcm_frame(5, 10_000))

            self.assertEqual(recorder.sessions, 2)
            self.assertEqual(
                [frame.sequence for frame in recorder.frames],
                [0, 1, 4, 5],
            )

    def _service(
        self,
        activity_detector: AudioActivityDetector,
        voice_detector: VoiceActivityDetector,
        recorder: AudioRecorder,
        *,
        activity: ActivityConfig | None = None,
        recording: RecordingConfig | None = None,
    ) -> SoundRecordingService:
        logger = logging.getLogger(f"test.detection.{id(self)}")
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
        return SoundRecordingService(
            AudioFrameBroker(20),
            activity_detector,
            recorder,
            audio_config(),
            activity or activity_config(),
            recording or recording_config(),
            voice_detector=voice_detector,
            vad_config=vad_config(),
            logger=logger,
        )


if __name__ == "__main__":
    unittest.main()
