from __future__ import annotations

import unittest
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

from src.models import AppConfig, ConfigurationError


class RecordingConfigurationTests(unittest.TestCase):
    config_data: dict[str, Any]

    @classmethod
    def setUpClass(cls) -> None:
        cls.config_data = yaml.safe_load(
            Path("config.yaml").read_text(encoding="utf-8")
        )

    def test_rejects_invalid_recording_ranges(self) -> None:
        invalid_values = (
            ("bitrate_kbps", 0),
            ("minimum_duration_ms", -1),
            ("maximum_duration_seconds", 0),
            ("minimum_duration_ms", 3_600_001),
        )

        for key, value in invalid_values:
            data = deepcopy(self.config_data)
            data["recording"][key] = value
            with self.subTest(key=key, value=value):
                with self.assertRaises(ConfigurationError):
                    AppConfig.from_dict(data)

    def test_rejects_invalid_activity_ranges(self) -> None:
        invalid_values = (
            ("threshold_dbfs", -97.0),
            ("threshold_dbfs", 1.0),
            ("minimum_active_ms", 0),
            ("silence_timeout_ms", 0),
            ("pre_buffer_ms", -1),
            ("post_buffer_ms", -1),
            ("pre_buffer_ms", 3_600_001),
        )

        for key, value in invalid_values:
            data = deepcopy(self.config_data)
            data["activity"][key] = value
            with self.subTest(key=key, value=value):
                with self.assertRaises(ConfigurationError):
                    AppConfig.from_dict(data)

    def test_rejects_invalid_vad_configuration(self) -> None:
        invalid_values = (
            ("engine", "other"),
            ("aggressiveness", 4),
            ("minimum_speech_ms", 0),
        )

        for key, value in invalid_values:
            data = deepcopy(self.config_data)
            data["vad"][key] = value
            with self.subTest(key=key, value=value):
                with self.assertRaises(ConfigurationError):
                    AppConfig.from_dict(data)

    def test_vad_requires_supported_pcm_dimensions_when_enabled(self) -> None:
        data = deepcopy(self.config_data)
        data["audio"]["sample_rate"] = 44_100

        with self.assertRaises(ConfigurationError):
            AppConfig.from_dict(data)


if __name__ == "__main__":
    unittest.main()
