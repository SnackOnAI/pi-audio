from __future__ import annotations

import unittest
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

from src.models import AppConfig, ConfigurationError


class RecordingConfigurationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config_data: dict[str, Any] = yaml.safe_load(
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


if __name__ == "__main__":
    unittest.main()
