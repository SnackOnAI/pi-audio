"""Pause, start, or inspect paid transcription API usage."""

from __future__ import annotations

import argparse
from pathlib import Path

from src.transcription import transcription_pause_path


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


def set_paused(recording_directory: Path, paused: bool) -> None:
    marker = transcription_pause_path(recording_directory)
    if paused:
        marker.parent.mkdir(parents=True, exist_ok=True)
        temporary = marker.with_name(f"{marker.name}.part")
        temporary.write_text("paused\n", encoding="utf-8")
        temporary.replace(marker)
    else:
        marker.unlink(missing_ok=True)


def main() -> int:
    args = parse_arguments()
    recording_directory = args.recordings.expanduser().resolve()
    if args.action == "pause":
        set_paused(recording_directory, True)
    elif args.action == "start":
        set_paused(recording_directory, False)

    paused = transcription_pause_path(recording_directory).exists()
    print(f"Transcription API usage: {'PAUSED' if paused else 'RUNNING'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
