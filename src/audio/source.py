"""Audio source interface and the sole ALSA-backed implementation."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections import deque
from contextlib import suppress
from pathlib import Path

from .models import AudioFrame


class AudioSourceError(RuntimeError):
    """Raised when an audio source cannot start or stops unexpectedly."""


class AudioSource(ABC):
    """Asynchronous source of uniformly formatted PCM frames."""

    @abstractmethod
    async def start(self) -> None:
        """Acquire resources and begin capturing audio."""

    @abstractmethod
    async def read_frame(self) -> AudioFrame:
        """Return the next complete PCM frame."""

    @abstractmethod
    async def close(self) -> None:
        """Stop capture and release resources. Must be idempotent."""


class AlsaAudioSource(AudioSource):
    """Capture PCM from ALSA by supervising one ``arecord`` process."""

    _FORMATS = {1: "U8", 2: "S16_LE", 3: "S24_3LE", 4: "S32_LE"}

    def __init__(
        self,
        *,
        device: str,
        sample_rate: int,
        channels: int,
        sample_width_bytes: int,
        frames_per_chunk: int,
        executable: str | Path = "arecord",
    ) -> None:
        if sample_rate <= 0 or channels <= 0 or frames_per_chunk <= 0:
            raise ValueError("audio dimensions must be positive")
        if sample_width_bytes not in self._FORMATS:
            raise ValueError("sample_width_bytes must be between 1 and 4")

        self._device = device
        self._sample_rate = sample_rate
        self._channels = channels
        self._sample_width_bytes = sample_width_bytes
        self._frames_per_chunk = frames_per_chunk
        self._executable = str(executable)
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_tail: deque[str] = deque(maxlen=20)
        self._sequence = 0

    @property
    def chunk_size_bytes(self) -> int:
        return self._frames_per_chunk * self._channels * self._sample_width_bytes

    async def start(self) -> None:
        if self._process is not None:
            raise AudioSourceError("audio source is already started")

        arguments = (
            self._executable,
            "--quiet",
            "--device",
            self._device,
            "--format",
            self._FORMATS[self._sample_width_bytes],
            "--rate",
            str(self._sample_rate),
            "--channels",
            str(self._channels),
            "--file-type",
            "raw",
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *arguments,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise AudioSourceError(f"unable to start arecord: {exc}") from exc

        self._process = process
        self._sequence = 0
        self._stderr_tail.clear()
        self._stderr_task = asyncio.create_task(
            self._drain_stderr(process), name="arecord-stderr"
        )

    async def read_frame(self) -> AudioFrame:
        process = self._process
        if process is None or process.stdout is None:
            raise AudioSourceError("audio source is not started")

        try:
            data = await process.stdout.readexactly(self.chunk_size_bytes)
        except asyncio.IncompleteReadError as exc:
            detail = "; ".join(self._stderr_tail) or "no stderr output"
            raise AudioSourceError(
                f"arecord stopped before a complete audio frame "
                f"({len(exc.partial)}/{self.chunk_size_bytes} bytes): {detail}"
            ) from exc

        frame = AudioFrame(
            data=data,
            sequence=self._sequence,
            captured_at=asyncio.get_running_loop().time(),
            sample_rate=self._sample_rate,
            channels=self._channels,
            sample_width_bytes=self._sample_width_bytes,
        )
        self._sequence += 1
        return frame

    async def close(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return

        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    process.kill()
                await process.wait()

        if self._stderr_task is not None:
            with suppress(asyncio.CancelledError):
                await self._stderr_task
            self._stderr_task = None

    async def _drain_stderr(self, process: asyncio.subprocess.Process) -> None:
        if process.stderr is None:
            return
        while line := await process.stderr.readline():
            self._stderr_tail.append(line.decode("utf-8", errors="replace").strip())
