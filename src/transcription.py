"""Crash-safe speech screening and cloud transcription of recordings."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import webrtcvad  # type: ignore[import-untyped]

from .models import AudioConfig, RecordingConfig, TranscriptionConfig
from .transcription_control import transcription_is_paused


class TranscriptionError(RuntimeError):
    """Raised when a recording cannot be screened or transcribed safely."""


class TranscriptionPaused(TranscriptionError):
    """Raised when the operator has paused paid transcription requests."""


@dataclass(frozen=True, slots=True)
class SpeechScreenResult:
    speech: bool
    duration_seconds: float
    maximum_continuous_speech_ms: int


@dataclass(frozen=True, slots=True)
class TranscriptionResponse:
    text: str
    request_id: str | None = None
    usage: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class TranscriptRecord:
    status: str
    audio_file: str
    model: str | None
    language: str
    duration_seconds: float
    completed_at: str
    text_file: str | None
    request_ids: tuple[str, ...]
    usage: tuple[dict[str, Any], ...]
    maximum_continuous_speech_ms: int


class TranscriptionClient(Protocol):
    async def transcribe(self, audio_path: Path) -> TranscriptionResponse:
        """Transcribe one supported audio file."""


class SpeechScreener(Protocol):
    async def screen(self, audio_path: Path) -> SpeechScreenResult:
        """Return whether a finalized recording contains sustained speech."""

    async def stop(self) -> None:
        """Stop any active decoder process."""


class OpenAITranscriptionClient:
    """Small async adapter around the official OpenAI Python client."""

    def __init__(self, config: TranscriptionConfig) -> None:
        api_key = os.environ.get(config.api_key_environment)
        if not api_key:
            raise TranscriptionError(
                f"environment variable {config.api_key_environment!r} is not set"
            )
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:
            raise TranscriptionError("the 'openai' package is not installed") from exc

        self._client = AsyncOpenAI(api_key=api_key)
        self._config = config

    async def transcribe(self, audio_path: Path) -> TranscriptionResponse:
        try:
            response = await self._client.audio.transcriptions.create(
                file=audio_path,
                model=self._config.model,
                language=self._config.language,
                prompt=self._config.prompt or None,
                response_format="json",
                temperature=0,
            )
        except Exception as exc:
            raise TranscriptionError(f"OpenAI request failed: {exc}") from exc

        text = getattr(response, "text", "").strip()
        usage_object = getattr(response, "usage", None)
        usage = (
            usage_object.model_dump(mode="json")
            if usage_object is not None and hasattr(usage_object, "model_dump")
            else None
        )
        return TranscriptionResponse(
            text=text,
            request_id=getattr(response, "_request_id", None),
            usage=usage,
        )


class FfmpegSpeechScreener:
    """Decode an MP3 and apply lightweight WebRTC VAD without using ALSA."""

    def __init__(
        self,
        audio_config: AudioConfig,
        config: TranscriptionConfig,
        *,
        executable: str | Path = "ffmpeg",
    ) -> None:
        if (
            audio_config.sample_rate != 16_000
            or audio_config.channels != 1
            or audio_config.sample_width_bytes != 2
            or audio_config.chunk_duration_ms not in (10, 20, 30)
        ):
            raise ValueError(
                "speech screening requires 16 kHz mono signed 16-bit PCM in "
                "10, 20, or 30 ms frames"
            )
        self._audio_config = audio_config
        self._config = config
        self._executable = str(executable)
        self._process: asyncio.subprocess.Process | None = None

    async def screen(self, audio_path: Path) -> SpeechScreenResult:
        arguments = (
            self._executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(audio_path),
            "-f",
            "s16le",
            "-ar",
            str(self._audio_config.sample_rate),
            "-ac",
            "1",
            "pipe:1",
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *arguments,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise TranscriptionError(f"unable to start FFmpeg: {exc}") from exc

        self._process = process
        assert process.stdout is not None
        vad = webrtcvad.Vad(self._config.vad_aggressiveness)
        chunk_size = (
            self._audio_config.frames_per_chunk * self._audio_config.sample_width_bytes
        )
        total_frames = 0
        continuous_speech_ms = 0
        maximum_speech_ms = 0
        try:
            while True:
                try:
                    data = await process.stdout.readexactly(chunk_size)
                except asyncio.IncompleteReadError as exc:
                    if exc.partial:
                        total_frames += len(exc.partial) // 2
                    break
                total_frames += self._audio_config.frames_per_chunk
                if vad.is_speech(data, self._audio_config.sample_rate):
                    continuous_speech_ms += self._audio_config.chunk_duration_ms
                    maximum_speech_ms = max(maximum_speech_ms, continuous_speech_ms)
                else:
                    continuous_speech_ms = 0

            _, stderr = await process.communicate()
        except asyncio.CancelledError:
            await self.stop()
            raise
        finally:
            if self._process is process:
                self._process = None

        if process.returncode != 0:
            detail = (stderr or b"").decode("utf-8", errors="replace").strip()
            raise TranscriptionError(
                f"FFmpeg exited with status {process.returncode}: "
                f"{detail or 'no stderr output'}"
            )
        return SpeechScreenResult(
            speech=maximum_speech_ms >= self._config.minimum_speech_ms,
            duration_seconds=total_frames / self._audio_config.sample_rate,
            maximum_continuous_speech_ms=maximum_speech_ms,
        )

    async def stop(self) -> None:
        process = self._process
        if process is None or process.returncode is not None:
            self._process = None
            return
        with suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except TimeoutError:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()
        finally:
            self._process = None


class RecordingTranscriptionService:
    """Discover finalized recordings and produce durable transcript sidecars."""

    _MAX_UPLOAD_BYTES = 24 * 1024 * 1024
    # Keep each response comfortably below the model's 2,000 output-token limit.
    _CHUNK_SECONDS = 10 * 60

    def __init__(
        self,
        recording_config: RecordingConfig,
        transcription_config: TranscriptionConfig,
        screener: SpeechScreener,
        client: TranscriptionClient,
        *,
        ffmpeg_executable: str | Path = "ffmpeg",
        logger: logging.Logger | None = None,
    ) -> None:
        self._recording_config = recording_config
        self._config = transcription_config
        self._screener = screener
        self._client = client
        self._ffmpeg_executable = str(ffmpeg_executable)
        self._logger = logger or logging.getLogger("audio_stack.transcription")
        self._task: asyncio.Task[None] | None = None
        self._process: asyncio.subprocess.Process | None = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.is_running:
            raise RuntimeError("transcription service is already running")
        self._recording_config.directory.mkdir(parents=True, exist_ok=True)
        self._task = asyncio.create_task(self._run(), name="recording-transcription")

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        task.cancel()
        try:
            with suppress(asyncio.CancelledError):
                await task
        finally:
            await self._screener.stop()
            await self._terminate_process()
            self._task = None

    async def wait(self) -> None:
        if self._task is not None:
            await asyncio.shield(self._task)

    async def _run(self) -> None:
        retry_delay = self._config.retry_initial_seconds
        while True:
            if self.is_paused:
                await asyncio.sleep(self._config.scan_interval_seconds)
                continue
            recordings = self._discover_recordings()
            if not recordings:
                await asyncio.sleep(self._config.scan_interval_seconds)
                continue
            failed = False
            paused = False
            for audio_path in recordings:
                try:
                    await self._process_recording(audio_path)
                except TranscriptionPaused:
                    paused = True
                    break
                except TranscriptionError as exc:
                    failed = True
                    self._logger.warning(
                        "Recording transcription failed",
                        extra={
                            "event": "transcription_failed",
                            "path": str(audio_path),
                            "retry_seconds": retry_delay,
                            "error": str(exc),
                        },
                    )
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(
                        retry_delay * 2,
                        self._config.retry_max_seconds,
                    )
                    break
                else:
                    retry_delay = self._config.retry_initial_seconds
            if not failed:
                await asyncio.sleep(self._config.scan_interval_seconds)

            if paused:
                self._logger.info(
                    "Transcription API usage is paused",
                    extra={"event": "transcription_paused"},
                )

    def _discover_recordings(self) -> list[Path]:
        now = time.time()
        paths: list[Path] = []
        for audio_path in self._recording_config.directory.glob("*.mp3"):
            if audio_path.name.endswith(".part.mp3"):
                continue
            if self._record_path(audio_path).exists():
                continue
            try:
                if now - audio_path.stat().st_mtime < self._config.settle_seconds:
                    continue
            except FileNotFoundError:
                continue
            paths.append(audio_path)
        return sorted(paths)

    async def _process_recording(self, audio_path: Path) -> None:
        screen = await self._screener.screen(audio_path)
        if not screen.speech:
            self._write_record(
                audio_path,
                TranscriptRecord(
                    status="no_speech",
                    audio_file=audio_path.name,
                    model=None,
                    language=self._config.language,
                    duration_seconds=screen.duration_seconds,
                    completed_at=datetime.now(timezone.utc).isoformat(),
                    text_file=None,
                    request_ids=(),
                    usage=(),
                    maximum_continuous_speech_ms=(screen.maximum_continuous_speech_ms),
                ),
            )
            self._logger.info(
                "Recording skipped because it contains no speech",
                extra={
                    "event": "transcription_no_speech",
                    "path": str(audio_path),
                },
            )
            return

        if not self._within_monthly_limit(screen.duration_seconds):
            raise TranscriptionError("monthly transcription audio-minute limit reached")

        if self.is_paused:
            raise TranscriptionPaused("transcription API usage is paused")

        responses: list[TranscriptionResponse] = []
        with tempfile.TemporaryDirectory(prefix="pi-audio-transcription-") as work:
            chunks = await self._prepare_chunks(
                audio_path,
                Path(work),
                screen.duration_seconds,
            )
            for chunk in chunks:
                try:
                    response = await asyncio.wait_for(
                        self._client.transcribe(chunk),
                        timeout=self._config.operation_timeout_seconds,
                    )
                except TimeoutError as exc:
                    raise TranscriptionError(
                        f"transcription timed out for {chunk.name}"
                    ) from exc
                responses.append(response)

        text = "\n\n".join(response.text.strip() for response in responses).strip()
        text_path = self._text_path(audio_path) if text else None
        if text_path is not None:
            self._write_text(audio_path, text)
        record = TranscriptRecord(
            status="completed" if text else "no_transcript",
            audio_file=audio_path.name,
            model=self._config.model,
            language=self._config.language,
            duration_seconds=screen.duration_seconds,
            completed_at=datetime.now(timezone.utc).isoformat(),
            text_file=text_path.name if text_path is not None else None,
            request_ids=tuple(
                response.request_id
                for response in responses
                if response.request_id is not None
            ),
            usage=tuple(
                response.usage for response in responses if response.usage is not None
            ),
            maximum_continuous_speech_ms=screen.maximum_continuous_speech_ms,
        )
        self._add_monthly_usage(screen.duration_seconds)
        self._write_record(audio_path, record)
        self._logger.info(
            "Recording transcribed" if text else "Recording contained no transcript",
            extra={
                "event": ("transcription_completed" if text else "transcription_empty"),
                "path": str(audio_path),
                "duration_seconds": screen.duration_seconds,
                "model": self._config.model,
            },
        )

    async def _prepare_chunks(
        self,
        audio_path: Path,
        work: Path,
        duration_seconds: float,
    ) -> list[Path]:
        if (
            duration_seconds <= self._CHUNK_SECONDS
            and audio_path.stat().st_size <= self._MAX_UPLOAD_BYTES
        ):
            return [audio_path]
        output_pattern = work / "chunk-%03d.mp3"
        arguments = (
            self._ffmpeg_executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(audio_path),
            "-c",
            "copy",
            "-f",
            "segment",
            "-segment_time",
            str(self._CHUNK_SECONDS),
            "-reset_timestamps",
            "1",
            str(output_pattern),
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *arguments,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise TranscriptionError(f"unable to start FFmpeg: {exc}") from exc
        self._process = process
        try:
            _, stderr = await process.communicate()
        except asyncio.CancelledError:
            await self._terminate_process()
            raise
        finally:
            if self._process is process:
                self._process = None
        if process.returncode != 0:
            detail = (stderr or b"").decode("utf-8", errors="replace").strip()
            raise TranscriptionError(
                f"FFmpeg chunking failed: {detail or process.returncode}"
            )
        chunks = sorted(work.glob("chunk-*.mp3"))
        if not chunks or any(
            path.stat().st_size > self._MAX_UPLOAD_BYTES for path in chunks
        ):
            raise TranscriptionError("recording could not be split below upload limit")
        return chunks

    async def _terminate_process(self) -> None:
        process = self._process
        if process is None or process.returncode is not None:
            self._process = None
            return
        with suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except TimeoutError:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()
        finally:
            self._process = None

    def _within_monthly_limit(self, duration_seconds: float) -> bool:
        used_seconds = self._read_monthly_usage()
        limit_seconds = self._config.max_monthly_audio_minutes * 60
        return used_seconds + duration_seconds <= limit_seconds

    def _read_monthly_usage(self) -> float:
        path = self._usage_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return float(data["audio_seconds"])
        except FileNotFoundError:
            return 0.0
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise TranscriptionError(
                f"invalid transcription usage ledger: {exc}"
            ) from exc

    def _add_monthly_usage(self, duration_seconds: float) -> None:
        used_seconds = self._read_monthly_usage() + duration_seconds
        path = self._usage_path()
        temporary = path.with_name(f"{path.name}.part")
        try:
            temporary.write_text(
                json.dumps(
                    {
                        "month": datetime.now(timezone.utc).strftime("%Y-%m"),
                        "audio_seconds": used_seconds,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    },
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            temporary.replace(path)
        except OSError as exc:
            raise TranscriptionError(f"unable to update usage ledger: {exc}") from exc

    def _usage_path(self) -> Path:
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        return self._recording_config.directory / f".transcription-usage-{month}.json"

    @property
    def is_paused(self) -> bool:
        return transcription_is_paused(self._recording_config.directory)

    def _write_text(self, audio_path: Path, text: str) -> None:
        path = self._text_path(audio_path)
        temporary = path.with_name(f"{path.name}.part")
        try:
            temporary.write_text(f"{text}\n", encoding="utf-8")
            temporary.replace(path)
        except OSError as exc:
            raise TranscriptionError(f"unable to save transcript text: {exc}") from exc

    def _write_record(self, audio_path: Path, record: TranscriptRecord) -> None:
        path = self._record_path(audio_path)
        temporary = path.with_name(f"{path.name}.part")
        try:
            temporary.write_text(
                json.dumps(asdict(record), separators=(",", ":")),
                encoding="utf-8",
            )
            temporary.replace(path)
        except OSError as exc:
            raise TranscriptionError(
                f"unable to save transcript record: {exc}"
            ) from exc

    @staticmethod
    def _text_path(audio_path: Path) -> Path:
        return audio_path.with_suffix(".txt")

    @staticmethod
    def _record_path(audio_path: Path) -> Path:
        return audio_path.with_suffix(".transcript.json")
