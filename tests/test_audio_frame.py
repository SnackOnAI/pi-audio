from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError

from src.audio.models import AudioFrame


class AudioFrameTests(unittest.TestCase):
    def test_exposes_pcm_dimensions(self) -> None:
        frame = AudioFrame(
            data=b"\x00\x01" * 480,
            sequence=7,
            captured_at=12.5,
            sample_rate=16_000,
            channels=1,
            sample_width_bytes=2,
        )

        self.assertEqual(frame.frame_count, 480)
        self.assertEqual(frame.duration_seconds, 0.03)

    def test_is_immutable(self) -> None:
        frame = AudioFrame(b"\x00\x00", 0, 0.0, 16_000, 1, 2)

        with self.assertRaises(FrozenInstanceError):
            frame.sequence = 1  # type: ignore[misc]

    def test_rejects_incomplete_pcm_frame(self) -> None:
        with self.assertRaisesRegex(ValueError, "complete interleaved"):
            AudioFrame(b"\x00", 0, 0.0, 16_000, 1, 2)

    def test_rejects_invalid_metadata(self) -> None:
        valid = (b"\x00\x00", 0, 0.0, 16_000, 1, 2)

        for index, value in ((1, -1), (2, -0.1), (3, 0), (4, 0), (5, 0)):
            values = list(valid)
            values[index] = value
            with self.subTest(index=index):
                with self.assertRaises(ValueError):
                    AudioFrame(*values)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
