from __future__ import annotations

import stat
import tempfile
import textwrap
import unittest
from pathlib import Path

from src.audio.source import AlsaAudioSource, AudioSourceError


class AlsaAudioSourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_fixed_size_sequential_frames(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = self._fake_arecord(Path(directory), chunks=2)
            source = AlsaAudioSource(
                device="test-device",
                sample_rate=100,
                channels=1,
                sample_width_bytes=2,
                frames_per_chunk=2,
                executable=executable,
            )

            await source.start()
            first = await source.read_frame()
            second = await source.read_frame()
            await source.close()

            arguments = (Path(directory) / "arguments").read_text(encoding="utf-8")

        self.assertEqual(first.data, b"\x01\x02\x01\x02")
        self.assertEqual(second.data, first.data)
        self.assertEqual((first.sequence, second.sequence), (0, 1))
        self.assertLessEqual(first.captured_at, second.captured_at)
        self.assertIn("--file-type raw", arguments)
        self.assertNotIn("--type", arguments.split())

    async def test_reports_early_arecord_exit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = self._fake_arecord(Path(directory), chunks=0)
            source = AlsaAudioSource(
                device="test-device",
                sample_rate=16_000,
                channels=1,
                sample_width_bytes=2,
                frames_per_chunk=480,
                executable=executable,
            )
            await source.start()

            with self.assertRaisesRegex(AudioSourceError, "complete audio frame"):
                await source.read_frame()
            await source.close()

    async def test_requires_start_before_read(self) -> None:
        source = AlsaAudioSource(
            device="test",
            sample_rate=16_000,
            channels=1,
            sample_width_bytes=2,
            frames_per_chunk=480,
        )
        with self.assertRaisesRegex(AudioSourceError, "not started"):
            await source.read_frame()

    @staticmethod
    def _fake_arecord(directory: Path, *, chunks: int) -> Path:
        executable = directory / "arecord"
        executable.write_text(
            textwrap.dedent(
                f"""\
                #!/usr/bin/env python3
                import sys
                import time
                from pathlib import Path

                Path(__file__).with_name("arguments").write_text(" ".join(sys.argv[1:]))
                sys.stdout.buffer.write(b"\\x01\\x02" * {chunks * 2})
                sys.stdout.buffer.flush()
                time.sleep({10 if chunks else 0})
                """
            ),
            encoding="utf-8",
        )
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
        return executable


if __name__ == "__main__":
    unittest.main()
