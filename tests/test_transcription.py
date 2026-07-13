from __future__ import annotations

import json
import tempfile
import unittest
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from src.models import AudioConfig, RecordingConfig, TranscriptionConfig
from src.transcription import (
    FfmpegSpeechScreener,
    RecordingTranscriptionService,
    SpeechScreenResult,
    TranscriptionError,
    TranscriptionResponse,
)


def recording_config(directory: Path) -> RecordingConfig:
    return RecordingConfig(directory, "mp3", 64, 500, 3600, True)


def transcription_config(
    *, max_monthly_audio_minutes: int = 1500
) -> TranscriptionConfig:
    return TranscriptionConfig(
        enabled=True,
        provider="openai",
        model="gpt-4o-transcribe",
        api_key_environment="OPENAI_API_KEY",
        language="en",
        prompt="Pi Audio test",
        scan_interval_seconds=1,
        settle_seconds=0,
        operation_timeout_seconds=30,
        retry_initial_seconds=1,
        retry_max_seconds=4,
        minimum_speech_ms=300,
        vad_aggressiveness=2,
        max_monthly_audio_minutes=max_monthly_audio_minutes,
    )


class FakeScreener:
    def __init__(self, result: SpeechScreenResult) -> None:
        self.result = result
        self.paths: list[Path] = []

    async def screen(self, audio_path: Path) -> SpeechScreenResult:
        self.paths.append(audio_path)
        return self.result

    async def stop(self) -> None:
        return None


class FakeClient:
    def __init__(self, response: TranscriptionResponse) -> None:
        self.response = response
        self.paths: list[Path] = []

    async def transcribe(self, audio_path: Path) -> TranscriptionResponse:
        self.paths.append(audio_path)
        return self.response


class FakeDecoderProcess:
    def __init__(self, pcm: bytes, returncode: int = 0) -> None:
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(pcm)
        self.stdout.feed_eof()
        self.returncode = returncode

    async def communicate(self) -> tuple[None, bytes]:
        return None, b""


class FfmpegSpeechScreenerTests(unittest.IsolatedAsyncioTestCase):
    async def test_requires_continuous_speech_before_cloud_processing(self) -> None:
        audio = AudioConfig("test", 16_000, 1, 2, 30, 250)
        config = transcription_config()
        screener = FfmpegSpeechScreener(audio, config)
        pcm = b"\0" * (audio.frames_per_chunk * 2 * 11)
        process = FakeDecoderProcess(pcm)
        vad = Mock()
        vad.is_speech.side_effect = [True] * 10 + [False]

        with (
            patch(
                "asyncio.create_subprocess_exec",
                AsyncMock(return_value=process),
            ) as create_process,
            patch("src.transcription.webrtcvad.Vad", return_value=vad),
        ):
            result = await screener.screen(Path("recording.mp3"))

        self.assertTrue(result.speech)
        self.assertAlmostEqual(result.duration_seconds, 0.33)
        self.assertEqual(result.maximum_continuous_speech_ms, 300)
        arguments = create_process.await_args.args
        self.assertEqual(arguments[0], "ffmpeg")
        self.assertIn("pipe:1", arguments)


class RecordingTranscriptionServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_speech_writes_durable_skip_record_without_cloud_call(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio_path = root / "sound-test.mp3"
            audio_path.write_bytes(b"mp3")
            screener = FakeScreener(SpeechScreenResult(False, 2.4, 120))
            client = FakeClient(TranscriptionResponse("unused"))
            service = RecordingTranscriptionService(
                recording_config(root),
                transcription_config(),
                screener,
                client,
            )

            await service._process_recording(audio_path)

            self.assertEqual(client.paths, [])
            record_path = audio_path.with_suffix(".transcript.json")
            record = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual(record["status"], "no_speech")
            self.assertEqual(record["maximum_continuous_speech_ms"], 120)
            self.assertFalse(audio_path.with_suffix(".txt").exists())

    async def test_speech_writes_text_record_and_monthly_usage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio_path = root / "sound-test.mp3"
            audio_path.write_bytes(b"mp3")
            screener = FakeScreener(SpeechScreenResult(True, 65.5, 900))
            client = FakeClient(
                TranscriptionResponse(
                    "Hello from the room.",
                    request_id="req_test",
                    usage={"audio_tokens": 123},
                )
            )
            service = RecordingTranscriptionService(
                recording_config(root),
                transcription_config(),
                screener,
                client,
            )

            await service._process_recording(audio_path)

            self.assertEqual(
                audio_path.with_suffix(".txt").read_text(encoding="utf-8"),
                "Hello from the room.\n",
            )
            record = json.loads(
                audio_path.with_suffix(".transcript.json").read_text(encoding="utf-8")
            )
            self.assertEqual(record["status"], "completed")
            self.assertEqual(record["model"], "gpt-4o-transcribe")
            self.assertEqual(record["request_ids"], ["req_test"])
            ledgers = list(root.glob(".transcription-usage-*.json"))
            self.assertEqual(len(ledgers), 1)
            ledger = json.loads(ledgers[0].read_text(encoding="utf-8"))
            self.assertEqual(ledger["audio_seconds"], 65.5)

    async def test_monthly_limit_preserves_recording_for_later_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio_path = root / "sound-test.mp3"
            audio_path.write_bytes(b"mp3")
            screener = FakeScreener(SpeechScreenResult(True, 61.0, 600))
            client = FakeClient(TranscriptionResponse("unused"))
            service = RecordingTranscriptionService(
                recording_config(root),
                transcription_config(max_monthly_audio_minutes=1),
                screener,
                client,
            )

            with self.assertRaisesRegex(TranscriptionError, "monthly"):
                await service._process_recording(audio_path)

            self.assertTrue(audio_path.exists())
            self.assertEqual(client.paths, [])
            self.assertFalse(audio_path.with_suffix(".transcript.json").exists())

    def test_discovery_ignores_partial_completed_and_unsettled_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ready = root / "ready.mp3"
            ready.write_bytes(b"ready")
            (root / "active.part.mp3").write_bytes(b"partial")
            completed = root / "completed.mp3"
            completed.write_bytes(b"done")
            completed.with_suffix(".transcript.json").write_text("{}", encoding="utf-8")
            service = RecordingTranscriptionService(
                recording_config(root),
                transcription_config(),
                FakeScreener(SpeechScreenResult(False, 1.0, 0)),
                FakeClient(TranscriptionResponse("unused")),
            )

            self.assertEqual(service._discover_recordings(), [ready])


if __name__ == "__main__":
    unittest.main()
