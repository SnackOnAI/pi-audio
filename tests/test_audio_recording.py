from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src.audio.models import AudioFrame
from src.audio.recording import AudioRecordingError, FfmpegAudioRecorder
from src.models import AudioConfig, RecordingConfig


class FakeStdin:
    def __init__(self, process: "FakeProcess") -> None:
        self._process = process
        self.data = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.data.extend(data)

    async def drain(self) -> None:
        return

    def close(self) -> None:
        self.closed = True
        self._process.finish()

    async def wait_closed(self) -> None:
        return


class FakeProcess:
    def __init__(self, returncode: int = 0) -> None:
        self._configured_returncode = returncode
        self.returncode: int | None = None
        self.stderr = asyncio.StreamReader()
        self.stdin = FakeStdin(self)
        self._finished = asyncio.Event()

    async def wait(self) -> int:
        await self._finished.wait()
        return self.returncode or 0

    def terminate(self) -> None:
        self.finish()

    def kill(self) -> None:
        self.finish()

    def finish(self) -> None:
        if self.returncode is not None:
            return
        self.returncode = self._configured_returncode
        self.stderr.feed_eof()
        self._finished.set()


def audio_config() -> AudioConfig:
    return AudioConfig(
        device="test",
        sample_rate=16_000,
        channels=1,
        sample_width_bytes=2,
        chunk_duration_ms=30,
        queue_size=10,
    )


def recording_config(
    directory: Path,
    *,
    minimum_duration_ms: int = 30,
    maximum_duration_seconds: int = 10,
) -> RecordingConfig:
    return RecordingConfig(
        directory=directory,
        format="mp3",
        bitrate_kbps=64,
        minimum_duration_ms=minimum_duration_ms,
        maximum_duration_seconds=maximum_duration_seconds,
        metadata_enabled=True,
    )


def frame(*, sample_rate: int = 16_000) -> AudioFrame:
    return AudioFrame(b"\x01\x02" * 480, 0, 1.0, sample_rate, 1, 2)


class FfmpegAudioRecorderTests(unittest.IsolatedAsyncioTestCase):
    async def test_commits_mp3_and_metadata_after_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            process = FakeProcess()
            recorder = FfmpegAudioRecorder(audio_config(), recording_config(root))

            with patch(
                "asyncio.create_subprocess_exec",
                AsyncMock(return_value=process),
            ):
                session = await recorder.start()
                temporary_path = session.path.with_suffix(".part.mp3")
                temporary_path.write_bytes(b"encoded-mp3")
                await recorder.write_frame(frame())
                result = await recorder.finish()

            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result.path.read_bytes(), b"encoded-mp3")
            self.assertEqual(result.duration_seconds, 0.03)
            self.assertEqual(result.pcm_frame_count, 480)
            self.assertFalse(temporary_path.exists())
            metadata = json.loads(
                result.path.with_suffix(".json").read_text(encoding="utf-8")
            )
            self.assertEqual(metadata["recording_id"], result.recording_id)
            self.assertEqual(metadata["size_bytes"], len(b"encoded-mp3"))
            self.assertFalse(recorder.is_recording)

    async def test_discards_recording_below_minimum_duration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            process = FakeProcess()
            recorder = FfmpegAudioRecorder(
                audio_config(),
                recording_config(Path(directory), minimum_duration_ms=31),
            )

            with patch(
                "asyncio.create_subprocess_exec",
                AsyncMock(return_value=process),
            ):
                session = await recorder.start()
                temporary_path = session.path.with_suffix(".part.mp3")
                temporary_path.write_bytes(b"encoded-mp3")
                await recorder.write_frame(frame())
                result = await recorder.finish()

            self.assertIsNone(result)
            self.assertFalse(session.path.exists())
            self.assertFalse(temporary_path.exists())

    async def test_abort_removes_partial_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            process = FakeProcess()
            recorder = FfmpegAudioRecorder(
                audio_config(), recording_config(Path(directory))
            )

            with patch(
                "asyncio.create_subprocess_exec",
                AsyncMock(return_value=process),
            ):
                session = await recorder.start()
                temporary_path = session.path.with_suffix(".part.mp3")
                temporary_path.write_bytes(b"partial")
                await recorder.abort()
                await recorder.abort()

            self.assertFalse(temporary_path.exists())
            self.assertFalse(session.path.exists())
            self.assertFalse(recorder.is_recording)

    async def test_rejects_mismatched_frame_format(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            recorder = FfmpegAudioRecorder(
                audio_config(), recording_config(Path(directory))
            )
            with patch(
                "asyncio.create_subprocess_exec",
                AsyncMock(return_value=FakeProcess()),
            ):
                await recorder.start()
                with self.assertRaisesRegex(AudioRecordingError, "does not match"):
                    await recorder.write_frame(frame(sample_rate=48_000))
                await recorder.abort()

    async def test_ffmpeg_failure_removes_partial_recording(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            process = FakeProcess(returncode=1)
            process.stderr.feed_data(b"encoding failed\n")
            recorder = FfmpegAudioRecorder(
                audio_config(), recording_config(Path(directory))
            )
            with patch(
                "asyncio.create_subprocess_exec",
                AsyncMock(return_value=process),
            ):
                session = await recorder.start()
                temporary_path = session.path.with_suffix(".part.mp3")
                temporary_path.write_bytes(b"partial")
                await recorder.write_frame(frame())

                with self.assertRaisesRegex(AudioRecordingError, "encoding failed"):
                    await recorder.finish()

            self.assertFalse(temporary_path.exists())
            self.assertFalse(session.path.exists())
            self.assertFalse(recorder.is_recording)

    async def test_rejects_frame_beyond_maximum_duration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            recorder = FfmpegAudioRecorder(
                audio_config(),
                recording_config(Path(directory), maximum_duration_seconds=1),
            )
            with patch(
                "asyncio.create_subprocess_exec",
                AsyncMock(return_value=FakeProcess()),
            ):
                await recorder.start()
                for _ in range(33):
                    await recorder.write_frame(frame())

                with self.assertRaisesRegex(AudioRecordingError, "maximum duration"):
                    await recorder.write_frame(frame())
                await recorder.abort()

    def test_builds_raw_pcm_to_mp3_command(self) -> None:
        recorder = FfmpegAudioRecorder(
            audio_config(), recording_config(Path("recordings"))
        )

        arguments = recorder._build_arguments(Path("recording.part.mp3"))

        self.assertIn("s16le", arguments)
        self.assertIn("pipe:0", arguments)
        self.assertIn("libmp3lame", arguments)
        self.assertIn("64k", arguments)
        self.assertEqual(arguments[-1], "recording.part.mp3")


if __name__ == "__main__":
    unittest.main()
