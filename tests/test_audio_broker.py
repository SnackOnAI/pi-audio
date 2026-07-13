from __future__ import annotations

import unittest

from src.audio.broker import AudioFrameBroker, AudioFrameBrokerFull
from src.audio.models import AudioFrame


def make_frame(sequence: int = 0) -> AudioFrame:
    return AudioFrame(b"\x00\x00", sequence, 1.0, 16_000, 1, 2)


class AudioFrameBrokerTests(unittest.IsolatedAsyncioTestCase):
    async def test_fans_out_same_immutable_frame(self) -> None:
        broker = AudioFrameBroker(queue_size=2)
        first = broker.subscribe()
        second = broker.subscribe()
        frame = make_frame()

        broker.publish(frame)

        self.assertIs(await first.__anext__(), frame)
        self.assertIs(await second.__anext__(), frame)

    async def test_closed_subscription_receives_no_more_frames(self) -> None:
        broker = AudioFrameBroker(queue_size=1)
        subscription = broker.subscribe()
        subscription.close()

        broker.publish(make_frame())

        self.assertEqual(broker.subscriber_count, 0)
        with self.assertRaises(StopAsyncIteration):
            await subscription.__anext__()

    async def test_fails_instead_of_silently_dropping_audio(self) -> None:
        broker = AudioFrameBroker(queue_size=1)
        broker.subscribe()
        broker.publish(make_frame())

        with self.assertRaises(AudioFrameBrokerFull):
            broker.publish(make_frame(1))


if __name__ == "__main__":
    unittest.main()
