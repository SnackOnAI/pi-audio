"""Immutable models used by the PCM audio pipeline."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AudioFrame:
    """A sequential chunk of interleaved PCM audio.

    ``captured_at`` is a monotonic-clock timestamp, rather than wall-clock time,
    so elapsed-time calculations are not affected by system clock changes.
    """

    data: bytes
    sequence: int
    captured_at: float
    sample_rate: int
    channels: int
    sample_width_bytes: int

    def __post_init__(self) -> None:
        if not isinstance(self.data, bytes):
            raise TypeError("data must be bytes")
        if not self.data:
            raise ValueError("data must not be empty")
        if self.sequence < 0:
            raise ValueError("sequence must not be negative")
        if self.captured_at < 0:
            raise ValueError("captured_at must not be negative")
        if self.sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        if self.channels <= 0:
            raise ValueError("channels must be positive")
        if self.sample_width_bytes <= 0:
            raise ValueError("sample_width_bytes must be positive")
        if len(self.data) % self.bytes_per_pcm_frame:
            raise ValueError(
                "data length must contain complete interleaved PCM frames"
            )

    @property
    def bytes_per_pcm_frame(self) -> int:
        return self.channels * self.sample_width_bytes

    @property
    def frame_count(self) -> int:
        return len(self.data) // self.bytes_per_pcm_frame

    @property
    def duration_seconds(self) -> float:
        return self.frame_count / self.sample_rate
