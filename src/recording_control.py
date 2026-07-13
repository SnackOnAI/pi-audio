"""Durable runtime control for local recording creation."""

from __future__ import annotations

from pathlib import Path


def recording_pause_path(recording_directory: Path) -> Path:
    """Return the durable runtime pause marker for recording."""
    return recording_directory / ".recording-paused"


def set_recording_paused(recording_directory: Path, paused: bool) -> None:
    """Atomically pause recording, or remove the pause marker."""
    marker = recording_pause_path(recording_directory)
    if paused:
        marker.parent.mkdir(parents=True, exist_ok=True)
        temporary = marker.with_name(f"{marker.name}.part")
        temporary.write_text("paused\n", encoding="utf-8")
        temporary.replace(marker)
    else:
        marker.unlink(missing_ok=True)


def recording_is_paused(recording_directory: Path) -> bool:
    """Return whether local recording creation is paused."""
    return recording_pause_path(recording_directory).exists()
