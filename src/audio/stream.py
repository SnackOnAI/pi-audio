"""FFmpeg-supervised live streaming from the audio frame broker."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from ..models import AudioConfig, StreamConfig
from .broker import AudioFrameBroker
from .models import AudioFrame


class AudioStreamingError(RuntimeError):
    """Raised when FFmpeg cannot stream brokered PCM audio."""


class FfmpegStreamingService:
    """Encode brokered PCM and continuously publish it to Icecast."""

    _INPUT_FORMATS = {1: "u8", 2: "s16le", 3: "s24le", 4: "s32le"}

    def __init__(
        self,
        broker: AudioFrameBroker,
        audio_config: AudioConfig,
        stream_config: StreamConfig,
        *,
        executable: str | Path = "ffmpeg",
        logger: logging.Logger | None = None,
    ) -> None:
        if stream_config.encoder.lower() != "ffmpeg":
            raise ValueError("stream.encoder must be 'ffmpeg'")
        if audio_config.sample_width_bytes not in self._INPUT_FORMATS:
            raise ValueError("unsupported PCM sample width")

        self._broker = broker
        self._audio_config = audio_config
        self._stream_config = stream_config
        self._executable = str(executable)
        self._logger = logger or logging.getLogger("audio_stack.stream")
        self._stop_requested = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_tail: deque[str] = deque(maxlen=20)
        self._write_timeout_seconds = max(
            1.0,
            audio_config.queue_size * audio_config.chunk_duration_ms / 2_000,
        )
        (
            self._destination_password,
            self._safe_destination,
        ) = self._parse_destination(stream_config.icecast_url)

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.is_running:
            raise RuntimeError("streaming service is already running")
        self._stop_requested.clear()
        self._task = asyncio.create_task(self._run(), name="ffmpeg-streaming")

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        self._stop_requested.set()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        self._task = None

    async def wait(self) -> None:
        if self._task is not None:
            await asyncio.shield(self._task)

    async def _run(self) -> None:
        while not self._stop_requested.is_set():
            try:
                await self._stream_once()
            except asyncio.CancelledError:
                raise
            except (AudioStreamingError, OSError) as exc:
                self._logger.error(
                    "FFmpeg stream failed",
                    extra={
                        "event": "stream_failed",
                        "error": str(exc),
                        "restart_delay_seconds": (
                            self._stream_config.restart_delay_seconds
                        ),
                    },
                )

            if not self._stop_requested.is_set():
                try:
                    await asyncio.wait_for(
                        self._stop_requested.wait(),
                        timeout=self._stream_config.restart_delay_seconds,
                    )
                except TimeoutError:
                    pass

    async def _stream_once(self) -> None:
        arguments = self._build_arguments()
        try:
            process = await asyncio.create_subprocess_exec(
                *arguments,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise AudioStreamingError(f"unable to start FFmpeg: {exc}") from exc

        self._process = process
        self._stderr_tail.clear()
        stderr_task = asyncio.create_task(
            self._drain_stderr(process), name="ffmpeg-stderr"
        )
        subscription = self._broker.subscribe()
        self._logger.info(
            "FFmpeg stream process started",
            extra={
                "event": "stream_process_started",
                "destination": self._safe_destination,
            },
        )

        try:
            async for frame in subscription:
                self._validate_frame(frame)
                await self._write_frame(process, frame)
        finally:
            subscription.close()
            await self._close_process(process)
            with suppress(asyncio.CancelledError):
                await stderr_task
            self._process = None

    async def _write_frame(
        self, process: asyncio.subprocess.Process, frame: AudioFrame
    ) -> None:
        if process.stdin is None:
            raise AudioStreamingError("FFmpeg stdin is unavailable")
        if process.returncode is not None:
            raise self._process_exit_error(process.returncode)
        try:
            process.stdin.write(frame.data)
            await asyncio.wait_for(
                process.stdin.drain(), timeout=self._write_timeout_seconds
            )
        except TimeoutError as exc:
            raise AudioStreamingError("FFmpeg stdin stopped accepting audio") from exc
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise self._process_exit_error(process.returncode) from exc

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
            raise AudioStreamingError(
                f"audio frame format {actual} does not match stream input {expected}"
            )

    async def _close_process(self, process: asyncio.subprocess.Process) -> None:
        if process.stdin is not None:
            process.stdin.close()
            with suppress(BrokenPipeError, ConnectionResetError):
                await process.stdin.wait_closed()

        if process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=2.0)
                except TimeoutError:
                    with suppress(ProcessLookupError):
                        process.kill()
                    await process.wait()

    async def _drain_stderr(self, process: asyncio.subprocess.Process) -> None:
        if process.stderr is None:
            return
        while line := await process.stderr.readline():
            self._stderr_tail.append(line.decode("utf-8", errors="replace").strip())

    def _process_exit_error(self, returncode: int | None) -> AudioStreamingError:
        detail = "; ".join(self._stderr_tail) or "no stderr output"
        return AudioStreamingError(f"FFmpeg exited with status {returncode}: {detail}")

    def _build_arguments(self) -> tuple[str, ...]:
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
            f"{self._stream_config.bitrate_kbps}k",
            "-content_type",
            "audio/mpeg",
            "-password",
            self._destination_password,
            "-f",
            "mp3",
            self._safe_destination,
        )

    @staticmethod
    def _parse_destination(destination: str) -> tuple[str, str]:
        parts = urlsplit(destination)
        if parts.scheme != "icecast" or not parts.hostname or not parts.path:
            raise ValueError("stream.icecast_url must be a valid Icecast URL")
        if parts.username not in (None, "source"):
            raise ValueError("stream.icecast_url username must be 'source'")
        password = parts.password
        if not password:
            raise ValueError("stream.icecast_url must include a source password")

        hostname = parts.hostname or ""
        if ":" in hostname:
            hostname = f"[{hostname}]"
        authority = hostname
        if parts.port is not None:
            authority = f"{authority}:{parts.port}"
        safe_destination = urlunsplit(
            (parts.scheme, authority, parts.path, parts.query, parts.fragment)
        )
        return password, safe_destination
