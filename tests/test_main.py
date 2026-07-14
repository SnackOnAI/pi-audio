from __future__ import annotations

import asyncio
import logging
import os
import signal
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

from src.audio import AudioGain
from src.audio.source import AudioSourceError
from src.config import load_config
from src.main import run_application
from src.models import AppConfig


class FakeService:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.started = False
        self.stopped = False
        self._finished = asyncio.Event()

    @property
    def is_running(self) -> bool:
        return self.started and not self.stopped

    async def start(self) -> None:
        self.started = True

    async def wait(self) -> None:
        if self.failure is not None:
            raise self.failure
        await self._finished.wait()

    async def stop(self) -> None:
        self.stopped = True
        self._finished.set()

    async def get_input_gain(self) -> AudioGain:
        return AudioGain(100, 22.0)

    async def set_input_gain(self, percent: int) -> AudioGain:
        return AudioGain(percent, None)


class ApplicationRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_signal_stops_capture_and_streaming(self) -> None:
        capture = FakeService()
        streaming = FakeService()
        detection = FakeService()
        upload = FakeService()
        transcription = FakeService()
        control_api = FakeService()
        callbacks: dict[signal.Signals, Callable[[], None]] = {}
        loop = asyncio.get_running_loop()

        def add_signal_handler(
            handled_signal: signal.Signals, callback: Callable[[], None]
        ) -> None:
            callbacks[handled_signal] = callback

        with (
            self._runtime_patches(
                capture,
                streaming,
                detection,
                upload,
                transcription,
                control_api,
            ),
            patch.object(loop, "add_signal_handler", side_effect=add_signal_handler),
            patch.object(loop, "remove_signal_handler", return_value=True),
        ):
            runtime = asyncio.create_task(
                run_application(self._config(), self._logger())
            )
            await self._wait_for(lambda: signal.SIGTERM in callbacks)
            callback = callbacks[signal.SIGTERM]
            callback()
            await asyncio.wait_for(runtime, timeout=1.0)

        self.assertTrue(capture.started)
        self.assertTrue(capture.stopped)
        self.assertTrue(streaming.started)
        self.assertTrue(streaming.stopped)
        self.assertTrue(detection.started)
        self.assertTrue(detection.stopped)
        self.assertTrue(upload.started)
        self.assertTrue(upload.stopped)
        self.assertTrue(transcription.started)
        self.assertTrue(transcription.stopped)
        self.assertTrue(control_api.started)
        self.assertTrue(control_api.stopped)

    async def test_capture_failure_still_stops_streaming(self) -> None:
        capture = FakeService(AudioSourceError("capture failed"))
        streaming = FakeService()
        detection = FakeService()
        upload = FakeService()
        transcription = FakeService()
        control_api = FakeService()
        loop = asyncio.get_running_loop()

        with (
            self._runtime_patches(
                capture,
                streaming,
                detection,
                upload,
                transcription,
                control_api,
            ),
            patch.object(loop, "add_signal_handler"),
            patch.object(loop, "remove_signal_handler", return_value=True),
        ):
            with self.assertRaisesRegex(AudioSourceError, "capture failed"):
                await run_application(self._config(), self._logger())

        self.assertTrue(capture.stopped)
        self.assertTrue(streaming.stopped)
        self.assertTrue(detection.stopped)
        self.assertTrue(upload.stopped)
        self.assertTrue(transcription.stopped)
        self.assertTrue(control_api.stopped)

    async def test_upload_failure_stops_all_services(self) -> None:
        capture = FakeService()
        streaming = FakeService()
        detection = FakeService()
        upload = FakeService(RuntimeError("upload failed"))
        transcription = FakeService()
        control_api = FakeService()
        loop = asyncio.get_running_loop()

        with (
            self._runtime_patches(
                capture,
                streaming,
                detection,
                upload,
                transcription,
                control_api,
            ),
            patch.object(loop, "add_signal_handler"),
            patch.object(loop, "remove_signal_handler", return_value=True),
        ):
            with self.assertRaisesRegex(RuntimeError, "upload failed"):
                await run_application(self._config(), self._logger())

        self.assertTrue(capture.stopped)
        self.assertTrue(streaming.stopped)
        self.assertTrue(detection.stopped)
        self.assertTrue(upload.stopped)
        self.assertTrue(transcription.stopped)
        self.assertTrue(control_api.stopped)

    def _runtime_patches(
        self,
        capture: FakeService,
        streaming: FakeService,
        detection: FakeService,
        upload: FakeService,
        transcription: FakeService,
        control_api: FakeService,
    ) -> "_RuntimePatches":
        return _RuntimePatches(
            capture,
            streaming,
            detection,
            upload,
            transcription,
            control_api,
        )

    def _config(self) -> AppConfig:
        with patch.dict(os.environ, {"ICECAST_SOURCE_PASSWORD": "test"}):
            return load_config(Path("config.yaml"))

    def _logger(self) -> logging.Logger:
        logger = logging.getLogger(f"test.runtime.{id(self)}")
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
        return logger

    async def _wait_for(self, predicate: Callable[[], bool]) -> None:
        for _ in range(100):
            if predicate():
                return
            await asyncio.sleep(0)
        self.fail("runtime did not install its signal handlers")


class _RuntimePatches:
    def __init__(
        self,
        capture: FakeService,
        streaming: FakeService,
        detection: FakeService,
        upload: FakeService,
        transcription: FakeService,
        control_api: FakeService,
    ) -> None:
        self._patches = (
            patch("src.main.AlsaAudioSource"),
            patch("src.main.AlsaAudioGainControl"),
            patch("src.main.AudioCaptureService", return_value=capture),
            patch("src.main.FfmpegStreamingService", return_value=streaming),
            patch("src.main.FfmpegAudioRecorder"),
            patch("src.main.RmsAudioActivityDetector"),
            patch("src.main.WebRtcVoiceActivityDetector"),
            patch("src.main.SoundRecordingService", return_value=detection),
            patch("src.main.RcloneUploadService", return_value=upload),
            patch("src.main.FfmpegSpeechScreener"),
            patch("src.main.OpenAITranscriptionClient"),
            patch(
                "src.main.RecordingTranscriptionService",
                return_value=transcription,
            ),
            patch("src.main.ControlApiService", return_value=control_api),
        )

    def __enter__(self) -> "_RuntimePatches":
        for active_patch in self._patches:
            active_patch.start()
        return self

    def __exit__(self, *args: object) -> None:
        for active_patch in reversed(self._patches):
            active_patch.stop()


if __name__ == "__main__":
    unittest.main()
