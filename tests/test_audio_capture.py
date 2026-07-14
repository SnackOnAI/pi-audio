from __future__ import annotations

import asyncio
import unittest

from src.audio.broker import AudioFrameBroker
from src.audio.capture import AudioCaptureService
from src.audio.gain import AudioGain, AudioGainControl
from src.audio.models import AudioFrame
from src.audio.source import AudioSource, AudioSourceError


class FakeAudioSource(AudioSource):
    def __init__(self, frames: list[AudioFrame]) -> None:
        self.frames = frames
        self.started = False
        self.closed = False
        self._closed = asyncio.Event()

    async def start(self) -> None:
        self.started = True

    async def read_frame(self) -> AudioFrame:
        if self.frames:
            return self.frames.pop(0)
        await self._closed.wait()
        raise AudioSourceError("closed")

    async def close(self) -> None:
        self.closed = True
        self._closed.set()


class FakeGainControl(AudioGainControl):
    def __init__(self) -> None:
        self.gain = AudioGain(percent=75, decibels=10.0)

    async def get(self) -> AudioGain:
        return self.gain

    async def set(self, percent: int) -> AudioGain:
        self.gain = AudioGain(percent=percent, decibels=1.0)
        return self.gain


class AudioCaptureServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_publishes_source_frames_and_stops_cleanly(self) -> None:
        frames = [
            AudioFrame(b"\x00\x00", index, float(index), 16_000, 1, 2)
            for index in range(2)
        ]
        source = FakeAudioSource(frames.copy())
        broker = AudioFrameBroker(queue_size=2)
        subscription = broker.subscribe()
        service = AudioCaptureService(source, broker)

        await service.start()
        received = [await subscription.__anext__(), await subscription.__anext__()]
        await service.stop()

        self.assertTrue(source.started)
        self.assertTrue(source.closed)
        self.assertEqual(received, frames)
        self.assertFalse(service.is_running)

    async def test_rejects_duplicate_start(self) -> None:
        source = FakeAudioSource([])
        service = AudioCaptureService(source, AudioFrameBroker(queue_size=1))
        await service.start()

        with self.assertRaisesRegex(RuntimeError, "already running"):
            await service.start()
        await service.stop()

    async def test_cancelling_waiter_does_not_cancel_capture(self) -> None:
        source = FakeAudioSource([])
        service = AudioCaptureService(source, AudioFrameBroker(queue_size=1))
        await service.start()
        waiter = asyncio.create_task(service.wait())
        await asyncio.sleep(0)

        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter

        self.assertTrue(service.is_running)
        await service.stop()

    async def test_gain_is_only_exposed_through_capture_service(self) -> None:
        gain_control = FakeGainControl()
        service = AudioCaptureService(
            FakeAudioSource([]),
            AudioFrameBroker(queue_size=1),
            gain_control=gain_control,
        )

        self.assertEqual((await service.get_input_gain()).percent, 75)
        self.assertEqual((await service.set_input_gain(40)).percent, 40)


if __name__ == "__main__":
    unittest.main()
