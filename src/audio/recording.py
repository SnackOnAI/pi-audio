"""FFmpeg-backed sound recording primitives."""

from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from collections import deque
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..models import AudioConfig, RecordingConfig
from .models import AudioFrame


class AudioRecordingError(RuntimeError):
    """Raised when a recording cannot be created safely."""


@dataclass(frozen=True, slots=True)
class RecordingSession:
    """Identity and destination of one active recording."""

    recording_id: str
    started_at: datetime
    path: Path


@dataclass(frozen=True, slots=True)
class RecordingResult:
    """Final metadata for one successfully committed recording."""

    recording_id: str
    path: Path
    started_at: datetime
    completed_at: datetime
    duration_seconds: float
    pcm_frame_count: int
    size_bytes: int


class AudioRecorder(ABC):
    """Minimal recording lifecycle used by sound detection."""

    @abstractmethod
    async def start(self) -> RecordingSession:
        """Start a new recording session."""

    @abstractmethod
    async def write_frame(self, frame: AudioFrame) -> None:
        """Append one PCM frame to the active recording."""

    @abstractmethod
    async def finish(self) -> RecordingResult | None:
        """Commit a valid recording, or discard one that is too short."""

    @abstractmethod
    async def abort(self) -> None:
        """Discard the active recording. Must be idempotent."""


class FfmpegAudioRecorder(AudioRecorder):
    """Encode explicitly bounded PCM frames into one atomic MP3 file."""

    _INPUT_FORMATS = {1: "u8", 2: "s16le", 3: "s24le", 4: "s32le"}

    def __init__(
        self,
        audio_config: AudioConfig,
        recording_config: RecordingConfig,
        *,
        executable: str | Path = "ffmpeg",
        logger: logging.Logger | None = None,
    ) -> None:
        if recording_config.format.lower() != "mp3":
            raise ValueError("only MP3 recording is supported")
        if audio_config.sample_width_bytes not in self._INPUT_FORMATS:
            raise ValueError("unsupported PCM sample width")

        self._audio_config = audio_config
        self._recording_config = recording_config
        self._executable = str(executable)
        self._logger = logger or logging.getLogger("audio_stack.recording")
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_tail: deque[str] = deque(maxlen=20)
        self._session: RecordingSession | None = None
        self._temporary_path: Path | None = None
        self._pcm_frame_count = 0
        self._write_timeout_seconds = max(
            1.0,
            audio_config.queue_size * audio_config.chunk_duration_ms / 2_000,
        )

    @property
    def is_recording(self) -> bool:
        return self._session is not None

    async def start(self) -> RecordingSession:
        if self.is_recording:
            raise AudioRecordingError("a recording is already active")

        started_at = datetime.now(timezone.utc)
        recording_id = uuid4().hex
        filename = f"sound-{started_at:%Y%m%dT%H%M%S.%fZ}-{recording_id[:8]}.mp3"
        directory = self._recording_config.directory
        directory.mkdir(parents=True, exist_ok=True)
        final_path = directory / filename
        temporary_path = final_path.with_suffix(".part.mp3")

        arguments = self._build_arguments(temporary_path)
        try:
            process = await asyncio.create_subprocess_exec(
                *arguments,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise AudioRecordingError(f"unable to start FFmpeg: {exc}") from exc

        self._process = process
        self._stderr_tail.clear()
        self._stderr_task = asyncio.create_task(
            self._drain_stderr(process), name="recording-ffmpeg-stderr"
        )
        self._temporary_path = temporary_path
        self._pcm_frame_count = 0
        self._session = RecordingSession(
            recording_id=recording_id,
            started_at=started_at,
            path=final_path,
        )
        self._logger.info(
            "Recording started",
            extra={
                "event": "recording_started",
                "recording_id": recording_id,
            },
        )
        return self._session

    async def write_frame(self, frame: AudioFrame) -> None:
        process = self._process
        if self._session is None or process is None or process.stdin is None:
            raise AudioRecordingError("no recording is active")
        self._validate_frame(frame)

        next_frame_count = self._pcm_frame_count + frame.frame_count
        if next_frame_count > self._maximum_pcm_frames:
            raise AudioRecordingError("recording maximum duration reached")
        if process.returncode is not None:
            raise self._process_exit_error(process.returncode)

        try:
            process.stdin.write(frame.data)
            await asyncio.wait_for(
                process.stdin.drain(), timeout=self._write_timeout_seconds
            )
        except TimeoutError as exc:
            raise AudioRecordingError("FFmpeg stdin stopped accepting audio") from exc
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise self._process_exit_error(process.returncode) from exc
        self._pcm_frame_count = next_frame_count

    async def finish(self) -> RecordingResult | None:
        if self._session is None:
            raise AudioRecordingError("no recording is active")

        session = self._session
        temporary_path = self._required_temporary_path
        pcm_frame_count = self._pcm_frame_count
        try:
            await self._finish_process()
            duration_seconds = pcm_frame_count / self._audio_config.sample_rate
            if duration_seconds < self._minimum_duration_seconds:
                temporary_path.unlink(missing_ok=True)
                self._logger.info(
                    "Recording discarded",
                    extra={
                        "event": "recording_discarded",
                        "recording_id": session.recording_id,
                        "duration_seconds": duration_seconds,
                    },
                )
                return None

            temporary_path.replace(session.path)
            result = RecordingResult(
                recording_id=session.recording_id,
                path=session.path,
                started_at=session.started_at,
                completed_at=datetime.now(timezone.utc),
                duration_seconds=duration_seconds,
                pcm_frame_count=pcm_frame_count,
                size_bytes=session.path.stat().st_size,
            )
            if self._recording_config.metadata_enabled:
                try:
                    self._write_metadata(result)
                except OSError:
                    self._logger.exception(
                        "Recording metadata could not be written",
                        extra={
                            "event": "recording_metadata_failed",
                            "recording_id": result.recording_id,
                        },
                    )
            self._logger.info(
                "Recording completed",
                extra={
                    "event": "recording_completed",
                    "recording_id": result.recording_id,
                    "path": str(result.path),
                    "duration_seconds": result.duration_seconds,
                    "size_bytes": result.size_bytes,
                },
            )
            return result
        except BaseException:
            await self._terminate_process()
            temporary_path.unlink(missing_ok=True)
            raise
        finally:
            self._reset()

    async def abort(self) -> None:
        if self._session is None:
            return
        temporary_path = self._temporary_path
        try:
            await self._terminate_process()
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            self._reset()

    async def _finish_process(self) -> None:
        process = self._required_process
        if process.stdin is not None:
            process.stdin.close()
            with suppress(BrokenPipeError, ConnectionResetError):
                await process.stdin.wait_closed()
        try:
            returncode = await asyncio.wait_for(process.wait(), timeout=10.0)
        except TimeoutError as exc:
            await self._terminate_process()
            self._required_temporary_path.unlink(missing_ok=True)
            raise AudioRecordingError("FFmpeg did not finish recording") from exc
        await self._wait_for_stderr()
        if returncode != 0:
            self._required_temporary_path.unlink(missing_ok=True)
            raise self._process_exit_error(returncode)
        if not self._required_temporary_path.is_file():
            raise AudioRecordingError("FFmpeg produced no recording file")

    async def _terminate_process(self) -> None:
        process = self._process
        if process is None:
            return
        if process.stdin is not None:
            process.stdin.close()
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
        await self._wait_for_stderr()

    async def _wait_for_stderr(self) -> None:
        if self._stderr_task is not None:
            with suppress(asyncio.CancelledError):
                await self._stderr_task
            self._stderr_task = None

    async def _drain_stderr(self, process: asyncio.subprocess.Process) -> None:
        if process.stderr is None:
            return
        while line := await process.stderr.readline():
            self._stderr_tail.append(line.decode("utf-8", errors="replace").strip())

    def _validate_frame(self, frame: AudioFrame) -> None:
        expected = (
            self._audio_config.sample_rate,
            self._audio_config.channels,
            self._audio_config.sample_width_bytes,
        )
        actual = (
            frame.sample_rate,
            frame.channels,
            frame.sample_width_bytes,
        )
        if actual != expected:
            raise AudioRecordingError(
                f"audio frame format {actual} does not match recorder input {expected}"
            )

    def _build_arguments(self, output_path: Path) -> tuple[str, ...]:
        return (
            self._executable,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-f",
            self._INPUT_FORMATS[self._audio_config.sample_width_bytes],
            "-ar",
            str(self._audio_config.sample_rate),
            "-ac",
            str(self._audio_config.channels),
            "-i",
            "pipe:0",
            "-vn",
            "-c:a",
            "libmp3lame",
            "-b:a",
            f"{self._recording_config.bitrate_kbps}k",
            "-f",
            "mp3",
            "-y",
            str(output_path),
        )

    def _write_metadata(self, result: RecordingResult) -> None:
        metadata_path = result.path.with_suffix(".json")
        temporary_path = metadata_path.with_suffix(".part.json")
        payload: dict[str, Any] = asdict(result)
        payload["path"] = str(result.path)
        payload["started_at"] = result.started_at.isoformat()
        payload["completed_at"] = result.completed_at.isoformat()
        temporary_path.write_text(
            json.dumps(payload, separators=(",", ":")), encoding="utf-8"
        )
        temporary_path.replace(metadata_path)

    def _process_exit_error(self, returncode: int | None) -> AudioRecordingError:
        detail = "; ".join(self._stderr_tail) or "no stderr output"
        return AudioRecordingError(f"FFmpeg exited with status {returncode}: {detail}")

    def _reset(self) -> None:
        self._process = None
        self._stderr_task = None
        self._session = None
        self._temporary_path = None
        self._pcm_frame_count = 0

    @property
    def _required_process(self) -> asyncio.subprocess.Process:
        if self._process is None:
            raise AudioRecordingError("recording process is unavailable")
        return self._process

    @property
    def _required_temporary_path(self) -> Path:
        if self._temporary_path is None:
            raise AudioRecordingError("recording temporary path is unavailable")
        return self._temporary_path

    @property
    def _minimum_duration_seconds(self) -> float:
        return self._recording_config.minimum_duration_ms / 1_000

    @property
    def _maximum_pcm_frames(self) -> int:
        return (
            self._recording_config.maximum_duration_seconds
            * self._audio_config.sample_rate
        )
