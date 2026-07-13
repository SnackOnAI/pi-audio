"""Crash-safe recording uploads supervised through rclone."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .models import RecordingConfig, UploadConfig


class UploadError(RuntimeError):
    """Raised when a recording bundle cannot be uploaded safely."""


@dataclass(frozen=True, slots=True)
class RecordingBundle:
    """Final recording files that share one recording identity."""

    audio_path: Path
    metadata_path: Path
    receipt_path: Path
    transcript_path: Path
    transcript_record_path: Path


@dataclass(frozen=True, slots=True)
class UploadReceipt:
    """Durable proof that every available bundle file was uploaded."""

    uploaded_at: str
    remote: str
    destination: str
    files: tuple[str, ...]


class RcloneUploadService:
    """Discover finalized recordings and upload them with bounded retries."""

    def __init__(
        self,
        recording_config: RecordingConfig,
        upload_config: UploadConfig,
        *,
        executable: str | Path = "rclone",
        logger: logging.Logger | None = None,
    ) -> None:
        self._recording_config = recording_config
        self._upload_config = upload_config
        self._executable = str(executable)
        self._logger = logger or logging.getLogger("audio_stack.upload")
        self._process: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.is_running:
            raise RuntimeError("upload service is already running")
        self._recording_config.directory.mkdir(parents=True, exist_ok=True)
        self._task = asyncio.create_task(self._run(), name="recording-upload")

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        task.cancel()
        try:
            with suppress(asyncio.CancelledError):
                await task
        finally:
            await self._terminate_process()
            self._task = None

    async def wait(self) -> None:
        if self._task is not None:
            await asyncio.shield(self._task)

    async def _run(self) -> None:
        retry_delay = self._upload_config.retry_initial_seconds
        while True:
            bundles = self._discover_bundles()
            if not bundles:
                await asyncio.sleep(self._upload_config.scan_interval_seconds)
                continue

            failed = False
            for bundle in bundles:
                try:
                    await self._process_bundle(bundle)
                except UploadError as exc:
                    failed = True
                    self._logger.warning(
                        "Recording upload failed",
                        extra={
                            "event": "upload_failed",
                            "path": str(bundle.audio_path),
                            "retry_seconds": retry_delay,
                            "error": str(exc),
                        },
                    )
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(
                        retry_delay * 2,
                        self._upload_config.retry_max_seconds,
                    )
                    break
                else:
                    retry_delay = self._upload_config.retry_initial_seconds

            if not failed:
                await asyncio.sleep(self._upload_config.scan_interval_seconds)

    def _discover_bundles(self) -> list[RecordingBundle]:
        directory = self._recording_config.directory
        stems: set[str] = set()
        now = time.time()

        for audio_path in directory.glob("*.mp3"):
            if audio_path.name.endswith(".part.mp3"):
                continue
            try:
                modified_at = audio_path.stat().st_mtime
            except FileNotFoundError:
                continue
            if now - modified_at < self._upload_config.settle_seconds:
                continue
            stems.add(audio_path.stem)

        for receipt_path in directory.glob("*.uploaded.json"):
            stems.add(receipt_path.name.removesuffix(".uploaded.json"))

        return [self._bundle(directory, stem) for stem in sorted(stems)]

    async def _process_bundle(self, bundle: RecordingBundle) -> None:
        if not bundle.audio_path.is_file():
            return
        paths = self._available_paths(bundle)
        uploaded_files = self._read_uploaded_files(bundle)
        pending_paths = [path for path in paths if path.name not in uploaded_files]

        if pending_paths:
            for path in pending_paths:
                await self._upload_file(path)
            try:
                self._write_receipt(bundle, paths)
            except OSError as exc:
                raise UploadError(
                    f"unable to save upload receipt for {bundle.audio_path.name}: {exc}"
                ) from exc
            self._logger.info(
                "Recording uploaded",
                extra={
                    "event": "upload_completed",
                    "path": str(bundle.audio_path),
                    "file_count": len(paths),
                },
            )

        if (
            self._upload_config.delete_after_success
            and self._ready_for_cleanup(bundle)
            and self._retention_elapsed(bundle)
        ):
            try:
                self._delete_uploaded_bundle(bundle)
            except OSError as exc:
                raise UploadError(
                    f"unable to delete uploaded bundle {bundle.audio_path.name}: {exc}"
                ) from exc

    async def _upload_file(self, path: Path) -> None:
        target = self._remote_target(path)
        arguments = (self._executable, "copyto", str(path), target)
        try:
            process = await asyncio.create_subprocess_exec(
                *arguments,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise UploadError(f"unable to start rclone: {exc}") from exc

        self._process = process
        try:
            _, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=self._upload_config.operation_timeout_seconds,
            )
        except TimeoutError as exc:
            await self._terminate_process()
            raise UploadError(f"rclone timed out uploading {path.name}") from exc
        except asyncio.CancelledError:
            await self._terminate_process()
            raise
        finally:
            if self._process is process:
                self._process = None

        if process.returncode != 0:
            detail = (stderr or b"").decode("utf-8", errors="replace").strip()
            raise UploadError(
                f"rclone exited with status {process.returncode}: "
                f"{detail or 'no stderr output'}"
            )

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

    def _write_receipt(self, bundle: RecordingBundle, paths: list[Path]) -> None:
        receipt = UploadReceipt(
            uploaded_at=datetime.now(timezone.utc).isoformat(),
            remote=self._upload_config.remote,
            destination=self._upload_config.destination,
            files=tuple(path.name for path in paths),
        )
        temporary_path = bundle.receipt_path.with_suffix(".part.json")
        temporary_path.write_text(
            json.dumps(asdict(receipt), separators=(",", ":")),
            encoding="utf-8",
        )
        temporary_path.replace(bundle.receipt_path)

    def _delete_uploaded_bundle(self, bundle: RecordingBundle) -> None:
        for path in self._available_paths(bundle):
            path.unlink(missing_ok=True)
        bundle.receipt_path.unlink(missing_ok=True)
        self._logger.info(
            "Uploaded local recording deleted",
            extra={
                "event": "upload_local_deleted",
                "path": str(bundle.audio_path),
            },
        )

    def _remote_target(self, path: Path) -> str:
        destination = self._upload_config.destination.strip("/")
        if self._upload_config.date_subdirectories:
            destination = f"{destination}/{self._date_subdirectory(path)}"
        return f"{self._upload_config.remote}:{destination}/{path.name}"

    @staticmethod
    def _date_subdirectory(path: Path) -> str:
        match = re.match(r"sound-(\d{4})(\d{2})(\d{2})T", path.name)
        if match is not None:
            return "/".join(match.groups())
        modified_at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        return modified_at.strftime("%Y/%m/%d")

    def _retention_elapsed(self, bundle: RecordingBundle) -> bool:
        retention_seconds = self._upload_config.local_retention_hours * 3_600
        try:
            uploaded_at = bundle.receipt_path.stat().st_mtime
        except FileNotFoundError:
            return False
        return time.time() - uploaded_at >= retention_seconds

    def _ready_for_cleanup(self, bundle: RecordingBundle) -> bool:
        return (
            not self._upload_config.wait_for_transcription
            or bundle.transcript_record_path.is_file()
        )

    @staticmethod
    def _available_paths(bundle: RecordingBundle) -> list[Path]:
        return [
            path
            for path in (
                bundle.audio_path,
                bundle.metadata_path,
                bundle.transcript_path,
                bundle.transcript_record_path,
            )
            if path.is_file()
        ]

    @staticmethod
    def _read_uploaded_files(bundle: RecordingBundle) -> set[str]:
        try:
            data = json.loads(bundle.receipt_path.read_text(encoding="utf-8"))
            files = data.get("files")
            if files is None:
                return {
                    path.name for path in RcloneUploadService._available_paths(bundle)
                }
            return {item for item in files if isinstance(item, str)}
        except (FileNotFoundError, OSError, json.JSONDecodeError, AttributeError):
            return set()

    @staticmethod
    def _bundle(directory: Path, stem: str) -> RecordingBundle:
        return RecordingBundle(
            audio_path=directory / f"{stem}.mp3",
            metadata_path=directory / f"{stem}.json",
            receipt_path=directory / f"{stem}.uploaded.json",
            transcript_path=directory / f"{stem}.txt",
            transcript_record_path=directory / f"{stem}.transcript.json",
        )
