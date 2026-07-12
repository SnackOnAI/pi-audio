"""Configuration loading and environment-variable expansion."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from .models import AppConfig, ConfigurationError

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_environment(value: Any) -> Any:
    """Recursively expand ${VARIABLE} references."""

    if isinstance(value, dict):
        return {
            key: _expand_environment(item)
            for key, item in value.items()
        }

    if isinstance(value, list):
        return [_expand_environment(item) for item in value]

    if not isinstance(value, str):
        return value

    def replace(match: re.Match[str]) -> str:
        variable = match.group(1)

        if variable not in os.environ:
            raise ConfigurationError(
                f"Required environment variable '{variable}' is not set."
            )

        return os.environ[variable]

    return _ENV_PATTERN.sub(replace, value)


def load_config(path: str | Path) -> AppConfig:
    """Load, expand and validate the YAML configuration file."""

    config_path = Path(path).expanduser().resolve()

    if not config_path.is_file():
        raise ConfigurationError(
            f"Configuration file does not exist: {config_path}"
        )

    try:
        raw_data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigurationError(
            f"Invalid YAML in {config_path}: {exc}"
        ) from exc
    except OSError as exc:
        raise ConfigurationError(
            f"Unable to read {config_path}: {exc}"
        ) from exc

    if not isinstance(raw_data, dict):
        raise ConfigurationError(
            "The root of config.yaml must be a mapping."
        )

    expanded_data = _expand_environment(raw_data)
    config = AppConfig.from_dict(expanded_data)

    # Resolve runtime paths relative to the configuration file.
    base_directory = config_path.parent

    object.__setattr__(
        config.recording,
        "directory",
        (base_directory / config.recording.directory).resolve(),
    )
    object.__setattr__(
        config.logging,
        "directory",
        (base_directory / config.logging.directory).resolve(),
    )
    object.__setattr__(
        config.health,
        "file",
        (base_directory / config.health.file).resolve(),
    )

    return config
