"""Pi Audio Stack application entry point."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import load_config
from .log_setup import configure_logging
from .models import ConfigurationError


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pi Audio Stack")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.yaml"),
        help="Path to the YAML configuration file.",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Validate configuration and exit.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()

    try:
        config = load_config(args.config)
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    logger = configure_logging(config.logging)

    logger.info(
        "Configuration loaded",
        extra={
            "event": "configuration_loaded",
            "application": config.application.name,
            "version": config.application.version,
            "config_path": str(args.config.resolve()),
            "audio_device": config.audio.device,
            "sample_rate": config.audio.sample_rate,
            "frames_per_chunk": config.audio.frames_per_chunk,
        },
    )

    if args.check_config:
        logger.info(
            "Configuration validation succeeded",
            extra={"event": "configuration_valid"},
        )
        return 0

    logger.info(
        "Application foundation started",
        extra={
            "event": "application_started",
            "note": "Audio workers are not enabled in Stage 2A.",
        },
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
