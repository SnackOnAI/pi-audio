"""Durable runtime control for paid transcription API requests."""

from __future__ import annotations

from pathlib import Path


def transcription_pause_path(recording_directory: Path) -> Path:
    """Return the durable runtime pause marker for transcription."""
    return recording_directory / ".transcription-paused"


def set_transcription_paused(recording_directory: Path, paused: bool) -> None:
    """Atomically pause transcription, or remove the pause marker."""
    marker = transcription_pause_path(recording_directory)
    if paused:
        marker.parent.mkdir(parents=True, exist_ok=True)
        temporary = marker.with_name(f"{marker.name}.part")
        temporary.write_text("paused\n", encoding="utf-8")
        temporary.replace(marker)
    else:
        marker.unlink(missing_ok=True)


def transcription_is_paused(recording_directory: Path) -> bool:
    """Return whether paid transcription requests are paused."""
    return transcription_pause_path(recording_directory).exists()
