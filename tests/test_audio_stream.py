from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from src.audio.broker import AudioFrameBroker
from src.audio.models import AudioFrame
from src.audio.stream import AudioStreamingError, FfmpegStreamingService
from src.models import AudioConfig, StreamConfig


class FakeStdin:
    def __init__(self) -> None:
        self.data = bytearray()
        self.written = asyncio.Event()
        self.closed = False
        self.on_close: object = None

    def write(self, data: bytes) -> None:
        self.data.extend(data)

    async def drain(self) -> None:
        self.written.set()

    def close(self) -> None:
        self.closed = True
        if callable(self.on_close):
            self.on_close()

    async def wait_closed(self) -> None:
        return


class FailingStdin(FakeStdin):
    async def drain(self) -> None:
        raise BrokenPipeError


class FakeProcess:
    def __init__(self, *, fail_writes: bool = False) -> None:
        self.stdin = FailingStdin() if fail_writes else FakeStdin()
        self.stdin.on_close = self._finish
        self.stderr = asyncio.StreamReader()
        self.returncode: int | None = None
        self._finished = asyncio.Event()

    async def wait(self) -> int:
        await self._finished.wait()
        return self.returncode or 0

    def terminate(self) -> None:
        self._finish()

    def kill(self) -> None:
        self._finish()

    def _finish(self) -> None:
        self.returncode = 0
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


def stream_config() -> StreamConfig:
    return StreamConfig(
        enabled=True,
        encoder="ffmpeg",
        bitrate_kbps=64,
        icecast_url="icecast://source:secret@127.0.0.1:8000/live.mp3",
        restart_delay_seconds=0,
    )


class FfmpegStreamingServiceTests(unittest.IsolatedAsyncioTestCase):
    def test_builds_raw_pcm_to_icecast_command_without_password_in_url(
        self,
    ) -> None:
        service = FfmpegStreamingService(
            AudioFrameBroker(10), audio_config(), stream_config()
        )

        arguments = service._build_arguments()

        self.assertIn("s16le", arguments)
        self.assertIn("pipe:0", arguments)
        self.assertIn("64k", arguments)
        self.assertEqual(arguments[-1], "icecast://127.0.0.1:8000/live.mp3")
        self.assertEqual(arguments[arguments.index("-password") + 1], "secret")

    async def test_writes_brokered_pcm_to_ffmpeg_stdin(self) -> None:
        broker = AudioFrameBroker(10)
        process = FakeProcess()
        service = FfmpegStreamingService(broker, audio_config(), stream_config())
        create_process = AsyncMock(return_value=process)

        with patch("asyncio.create_subprocess_exec", create_process):
            stream_task = asyncio.create_task(service._stream_once())
            await self._wait_for_subscriber(broker)
            frame = AudioFrame(b"\x01\x02" * 480, 0, 1.0, 16_000, 1, 2)
            broker.publish(frame)
            await asyncio.wait_for(process.stdin.written.wait(), timeout=1.0)
            stream_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await stream_task

        self.assertEqual(process.stdin.data, frame.data)
        self.assertTrue(process.stdin.closed)
        self.assertEqual(broker.subscriber_count, 0)

    def test_rejects_mismatched_audio_frame_format(self) -> None:
        service = FfmpegStreamingService(
            AudioFrameBroker(10), audio_config(), stream_config()
        )
        frame = AudioFrame(b"\x00\x00" * 2, 0, 1.0, 48_000, 1, 2)

        with self.assertRaisesRegex(AudioStreamingError, "does not match"):
            service._validate_frame(frame)

    def test_rejects_destination_without_source_password(self) -> None:
        invalid = stream_config()
        invalid = StreamConfig(
            enabled=invalid.enabled,
            encoder=invalid.encoder,
            bitrate_kbps=invalid.bitrate_kbps,
            icecast_url="icecast://127.0.0.1:8000/live.mp3",
            restart_delay_seconds=invalid.restart_delay_seconds,
        )

        with self.assertRaisesRegex(ValueError, "source password"):
            FfmpegStreamingService(AudioFrameBroker(10), audio_config(), invalid)

    async def test_restarts_with_a_fresh_subscription_after_failure(self) -> None:
        broker = AudioFrameBroker(10)
        failed_process = FakeProcess(fail_writes=True)
        replacement_process = FakeProcess()
        service = FfmpegStreamingService(broker, audio_config(), stream_config())
        create_process = AsyncMock(side_effect=[failed_process, replacement_process])
        frame = AudioFrame(b"\x01\x02" * 480, 0, 1.0, 16_000, 1, 2)

        with patch("asyncio.create_subprocess_exec", create_process):
            await service.start()
            await self._wait_for_subscriber(broker)
            broker.publish(frame)
            await self._wait_for_process_calls(create_process, 2)
            await self._wait_for_subscriber(broker)
            broker.publish(frame)
            await asyncio.wait_for(
                replacement_process.stdin.written.wait(), timeout=1.0
            )
            await service.stop()

        self.assertEqual(create_process.await_count, 2)
        self.assertEqual(replacement_process.stdin.data, frame.data)
        self.assertEqual(broker.subscriber_count, 0)

    async def _wait_for_subscriber(self, broker: AudioFrameBroker) -> None:
        for _ in range(100):
            if broker.subscriber_count:
                return
            await asyncio.sleep(0)
        self.fail("streaming service did not subscribe to the broker")

    async def _wait_for_process_calls(
        self, create_process: AsyncMock, count: int
    ) -> None:
        for _ in range(100):
            if create_process.await_count >= count:
                return
            await asyncio.sleep(0)
        self.fail(f"FFmpeg was not started {count} times")


if __name__ == "__main__":
    unittest.main()
