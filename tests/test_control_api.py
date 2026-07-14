from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from src.audio import AudioGain
from src.control_api import ControlApiError, ControlApiService
from src.models import ControlApiConfig, RecordingConfig, TranscriptionConfig
from src.recording_control import recording_is_paused
from src.transcription_control import transcription_is_paused


class ControlApiServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.recordings = Path(self.temporary.name) / "recordings"
        self.gain = AudioGain(percent=100, decibels=22.0)
        with patch.dict(os.environ, {"TEST_CONTROL_TOKEN": "secret-token"}):
            self.service = ControlApiService(
                ControlApiConfig(
                    enabled=True,
                    host="127.0.0.1",
                    port=8765,
                    token_environment="TEST_CONTROL_TOKEN",
                ),
                RecordingConfig(
                    directory=self.recordings,
                    format="mp3",
                    bitrate_kbps=64,
                    minimum_duration_ms=500,
                    maximum_duration_seconds=3600,
                    metadata_enabled=True,
                ),
                TranscriptionConfig(
                    enabled=True,
                    provider="openai",
                    model="gpt-4o-transcribe",
                    api_key_environment="OPENAI_API_KEY",
                    language="en",
                    prompt="",
                    scan_interval_seconds=10,
                    settle_seconds=2,
                    operation_timeout_seconds=600,
                    retry_initial_seconds=30,
                    retry_max_seconds=3600,
                    minimum_speech_ms=300,
                    vad_aggressiveness=2,
                    max_monthly_audio_minutes=1500,
                ),
                lambda: {
                    "capture": "running",
                    "stream": "running",
                    "recording": "running",
                    "transcription": "running",
                    "upload": "running",
                },
                gain_getter=self._get_gain,
                gain_setter=self._set_gain,
            )
        self.client = TestClient(TestServer(self.service._create_application()))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        self.temporary.cleanup()

    async def test_requires_bearer_authentication(self) -> None:
        response = await self.client.get("/api/v1/status")

        self.assertEqual(response.status, 401)
        self.assertEqual(await response.json(), {"error": "unauthorized"})

    async def test_reports_runtime_controls_and_gain(self) -> None:
        response = await self.client.get(
            "/api/v1/status", headers=self._authorization()
        )
        payload = await response.json()

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["service"], "running")
        self.assertEqual(payload["recording"], "running")
        self.assertEqual(payload["transcription"], "running")
        self.assertEqual(
            payload["gain"],
            {"available": True, "percent": 100, "decibels": 22.0},
        )
        self.assertEqual(payload["transcription_minutes_this_month"], 0.0)

    async def test_recording_and_transcription_controls_are_durable(self) -> None:
        recording_stop = await self.client.post(
            "/api/v1/recording/stop", headers=self._authorization()
        )
        transcription_stop = await self.client.post(
            "/api/v1/transcription/stop", headers=self._authorization()
        )

        self.assertEqual(recording_stop.status, 200)
        self.assertEqual(transcription_stop.status, 200)
        self.assertTrue(recording_is_paused(self.recordings))
        self.assertTrue(transcription_is_paused(self.recordings))

        await self.client.post("/api/v1/recording/start", headers=self._authorization())
        await self.client.post(
            "/api/v1/transcription/start", headers=self._authorization()
        )
        self.assertFalse(recording_is_paused(self.recordings))
        self.assertFalse(transcription_is_paused(self.recordings))

    async def test_sets_microphone_gain(self) -> None:
        response = await self.client.post(
            "/api/v1/gain",
            headers=self._authorization(),
            json={"percent": 45},
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(
            await response.json(),
            {"gain": {"available": True, "percent": 45, "decibels": 1.0}},
        )
        self.assertEqual(self.gain.percent, 45)

    async def test_rejects_out_of_range_gain(self) -> None:
        response = await self.client.post(
            "/api/v1/gain",
            headers=self._authorization(),
            json={"percent": 101},
        )

        self.assertEqual(response.status, 400)
        self.assertEqual(self.gain.percent, 100)

    async def _get_gain(self) -> AudioGain:
        return self.gain

    async def _set_gain(self, percent: int) -> AudioGain:
        self.gain = AudioGain(percent=percent, decibels=1.0)
        return self.gain

    @staticmethod
    def _authorization() -> dict[str, str]:
        return {"Authorization": "Bearer secret-token"}


class ControlApiConfigurationTests(unittest.TestCase):
    def test_requires_configured_token_environment_variable(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ControlApiError, "TEST_CONTROL_TOKEN"):
                ControlApiService(
                    ControlApiConfig(True, "127.0.0.1", 8765, "TEST_CONTROL_TOKEN"),
                    RecordingConfig(Path("recordings"), "mp3", 64, 500, 3600, True),
                    TranscriptionConfig(
                        True,
                        "openai",
                        "gpt-4o-transcribe",
                        "OPENAI_API_KEY",
                        "en",
                        "",
                        10,
                        2,
                        600,
                        30,
                        3600,
                        300,
                        2,
                        1500,
                    ),
                    lambda: {},
                )


if __name__ == "__main__":
    unittest.main()
