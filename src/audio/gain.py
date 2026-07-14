"""ALSA microphone gain control owned by the audio capture service."""

from __future__ import annotations

import asyncio
import re
from abc import ABC, abstractmethod
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path


class AudioGainError(RuntimeError):
    """Raised when microphone gain cannot be read or changed safely."""


@dataclass(frozen=True, slots=True)
class AudioGain:
    """Normalized microphone capture gain reported by ALSA."""

    percent: int
    decibels: float | None


class AudioGainControl(ABC):
    """Minimal gain interface exposed only through ``AudioCaptureService``."""

    @abstractmethod
    async def get(self) -> AudioGain:
        """Read the current capture gain."""

    @abstractmethod
    async def set(self, percent: int) -> AudioGain:
        """Set and return capture gain as a percentage."""


class AlsaAudioGainControl(AudioGainControl):
    """Run bounded ``amixer`` operations for one ALSA capture control."""

    _CAPTURE_PATTERN = re.compile(
        r"Capture\s+\d+\s+\[(?P<percent>\d+)%\]"
        r"(?:\s+\[(?P<decibels>-?inf|[+-]?\d+(?:\.\d+)?)dB\])?"
    )

    def __init__(
        self,
        *,
        device: str,
        control: str,
        operation_timeout_seconds: int,
        executable: str | Path = "amixer",
    ) -> None:
        if not device.strip() or not control.strip():
            raise ValueError("mixer device and control must not be empty")
        if operation_timeout_seconds <= 0:
            raise ValueError("operation timeout must be positive")
        self._device = device
        self._control = control
        self._timeout = operation_timeout_seconds
        self._executable = str(executable)
        self._lock = asyncio.Lock()

    async def get(self) -> AudioGain:
        async with self._lock:
            output = await self._run("sget", self._control)
            return self._parse(output)

    async def set(self, percent: int) -> AudioGain:
        if isinstance(percent, bool) or not isinstance(percent, int):
            raise ValueError("gain percent must be an integer")
        if not 0 <= percent <= 100:
            raise ValueError("gain percent must be between 0 and 100")
        async with self._lock:
            output = await self._run(
                "sset",
                self._control,
                f"{percent}%",
                "cap",
            )
            return self._parse(output)

    async def _run(self, *arguments: str) -> str:
        try:
            process = await asyncio.create_subprocess_exec(
                self._executable,
                "--device",
                self._device,
                *arguments,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise AudioGainError(f"unable to start amixer: {exc}") from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self._timeout
            )
        except TimeoutError as exc:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            raise AudioGainError("amixer operation timed out") from exc
        except asyncio.CancelledError:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
            raise

        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise AudioGainError(
                f"amixer exited with status {process.returncode}: "
                f"{detail or 'no stderr output'}"
            )
        return stdout.decode("utf-8", errors="replace")

    @classmethod
    def _parse(cls, output: str) -> AudioGain:
        match = cls._CAPTURE_PATTERN.search(output)
        if match is None:
            raise AudioGainError("amixer did not report a capture gain")
        decibels_text = match.group("decibels")
        decibels = None if decibels_text in (None, "-inf") else float(decibels_text)
        return AudioGain(
            percent=int(match.group("percent")),
            decibels=decibels,
        )
