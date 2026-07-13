from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.transcription_control import (
    set_transcription_paused,
    transcription_pause_path,
)


class TranscriptionControlTests(unittest.TestCase):
    def test_pause_and_start_are_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            recordings = Path(directory) / "recordings"
            marker = transcription_pause_path(recordings)

            set_transcription_paused(recordings, True)
            set_transcription_paused(recordings, True)
            self.assertTrue(marker.is_file())

            set_transcription_paused(recordings, False)
            set_transcription_paused(recordings, False)
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
