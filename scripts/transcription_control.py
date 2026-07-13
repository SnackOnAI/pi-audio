"""Pause, start, or inspect paid transcription API usage."""

from __future__ import annotations

import argparse
from pathlib import Path

from src.transcription_control import (
    set_transcription_paused,
    transcription_is_paused,
)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("pause", "start", "status"))
    parser.add_argument(
        "--recordings",
        type=Path,
        default=Path("recordings"),
        help="Recording directory (default: ./recordings)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()
    recording_directory = args.recordings.expanduser().resolve()
    if args.action == "pause":
        set_transcription_paused(recording_directory, True)
    elif args.action == "start":
        set_transcription_paused(recording_directory, False)

    paused = transcription_is_paused(recording_directory)
    print(f"Transcription API usage: {'PAUSED' if paused else 'RUNNING'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
