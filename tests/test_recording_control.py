from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.recording_control import (
    recording_is_paused,
    recording_pause_path,
    set_recording_paused,
)


class RecordingControlTests(unittest.TestCase):
    def test_pause_and_start_are_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            recordings = Path(directory) / "recordings"
            marker = recording_pause_path(recordings)

            set_recording_paused(recordings, True)
            set_recording_paused(recordings, True)
            self.assertTrue(marker.is_file())
            self.assertTrue(recording_is_paused(recordings))

            set_recording_paused(recordings, False)
            set_recording_paused(recordings, False)
            self.assertFalse(marker.exists())
            self.assertFalse(recording_is_paused(recordings))


if __name__ == "__main__":
    unittest.main()
