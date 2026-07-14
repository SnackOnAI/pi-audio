from __future__ import annotations

import stat
import tempfile
import textwrap
import unittest
from pathlib import Path

from src.audio.gain import AlsaAudioGainControl, AudioGainError


class AlsaAudioGainControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_samson_capture_gain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = self._fake_amixer(Path(directory))
            control = AlsaAudioGainControl(
                device="hw:CARD=Mic",
                control="Mic",
                operation_timeout_seconds=1,
                executable=executable,
            )

            gain = await control.get()
            arguments = executable.with_name("arguments").read_text(encoding="utf-8")

        self.assertEqual(gain.percent, 100)
        self.assertEqual(gain.decibels, 22.0)
        self.assertEqual(arguments, "--device hw:CARD=Mic sget Mic")

    async def test_sets_bounded_percentage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = self._fake_amixer(Path(directory), percent=50, decibels=1.0)
            control = AlsaAudioGainControl(
                device="hw:CARD=Mic",
                control="Mic",
                operation_timeout_seconds=1,
                executable=executable,
            )

            gain = await control.set(50)
            arguments = executable.with_name("arguments").read_text(encoding="utf-8")

        self.assertEqual(gain.percent, 50)
        self.assertEqual(gain.decibels, 1.0)
        self.assertEqual(arguments, "--device hw:CARD=Mic sset Mic 50% cap")

    async def test_rejects_invalid_percentage_before_running_amixer(self) -> None:
        control = AlsaAudioGainControl(
            device="hw:CARD=Mic",
            control="Mic",
            operation_timeout_seconds=1,
        )

        for value in (-1, 101, True, 50.5):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    await control.set(value)  # type: ignore[arg-type]

    async def test_reports_unparseable_amixer_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = self._fake_amixer(Path(directory), output="no capture value")
            control = AlsaAudioGainControl(
                device="hw:CARD=Mic",
                control="Mic",
                operation_timeout_seconds=1,
                executable=executable,
            )

            with self.assertRaisesRegex(AudioGainError, "capture gain"):
                await control.get()

    @staticmethod
    def _fake_amixer(
        directory: Path,
        *,
        percent: int = 100,
        decibels: float = 22.0,
        output: str | None = None,
    ) -> Path:
        executable = directory / "amixer"
        mixer_output = output or textwrap.dedent(f"""\
            Simple mixer control 'Mic',0
              Front Left: Capture 36 [{percent}%] [{decibels:.2f}dB] [on]
              Front Right: Capture 36 [{percent}%] [{decibels:.2f}dB] [on]
            """)
        executable.write_text(
            textwrap.dedent(f"""\
                #!/usr/bin/env python3
                import sys
                from pathlib import Path

                Path(__file__).with_name("arguments").write_text(" ".join(sys.argv[1:]))
                print({mixer_output!r})
                """),
            encoding="utf-8",
        )
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
        return executable


if __name__ == "__main__":
    unittest.main()
