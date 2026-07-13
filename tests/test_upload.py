from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src.models import RecordingConfig, UploadConfig
from src.upload import RcloneUploadService, UploadError


class FakeProcess:
    def __init__(self, returncode: int = 0, stderr: bytes = b"") -> None:
        self._configured_returncode = returncode
        self._stderr = stderr
        self.returncode: int | None = None
        self.terminated = False

    async def communicate(self) -> tuple[None, bytes]:
        self.returncode = self._configured_returncode
        return None, self._stderr

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = self._configured_returncode
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9


def recording_config(directory: Path) -> RecordingConfig:
    return RecordingConfig(directory, "mp3", 64, 500, 3600, True)


def upload_config(
    *,
    delete_after_success: bool,
    settle_seconds: int = 0,
    local_retention_hours: int = 0,
    wait_for_transcription: bool = False,
) -> UploadConfig:
    return UploadConfig(
        enabled=True,
        remote="dropbox-audio",
        destination="Pi Audio",
        scan_interval_seconds=1,
        settle_seconds=settle_seconds,
        operation_timeout_seconds=30,
        retry_initial_seconds=1,
        retry_max_seconds=4,
        delete_after_success=delete_after_success,
        local_retention_hours=local_retention_hours,
        wait_for_transcription=wait_for_transcription,
    )


class RcloneUploadServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_uploads_bundle_then_deletes_local_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio_path, metadata_path = self._write_bundle(root)
            service = RcloneUploadService(
                recording_config(root),
                upload_config(delete_after_success=True),
            )
            create_process = AsyncMock(side_effect=(FakeProcess(), FakeProcess()))

            with patch("asyncio.create_subprocess_exec", create_process):
                await service._process_bundle(service._bundle(root, audio_path.stem))

            self.assertEqual(create_process.await_count, 2)
            commands = [call.args for call in create_process.await_args_list]
            self.assertEqual(commands[0][:2], ("rclone", "copyto"))
            self.assertEqual(
                commands[0][3],
                f"dropbox-audio:Pi Audio/{audio_path.name}",
            )
            self.assertEqual(
                commands[1][3],
                f"dropbox-audio:Pi Audio/{metadata_path.name}",
            )
            self.assertFalse(audio_path.exists())
            self.assertFalse(metadata_path.exists())
            self.assertFalse(
                service._bundle(root, audio_path.stem).receipt_path.exists()
            )

    async def test_failed_bundle_keeps_every_local_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio_path, metadata_path = self._write_bundle(root)
            service = RcloneUploadService(
                recording_config(root),
                upload_config(delete_after_success=True),
            )

            with patch(
                "asyncio.create_subprocess_exec",
                AsyncMock(
                    side_effect=(
                        FakeProcess(),
                        FakeProcess(1, b"network unavailable"),
                    )
                ),
            ):
                with self.assertRaisesRegex(UploadError, "network unavailable"):
                    await service._process_bundle(
                        service._bundle(root, audio_path.stem)
                    )

            self.assertTrue(audio_path.exists())
            self.assertTrue(metadata_path.exists())
            self.assertFalse(
                service._bundle(root, audio_path.stem).receipt_path.exists()
            )

    async def test_receipt_prevents_duplicate_upload_when_files_are_retained(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio_path, _ = self._write_bundle(root)
            service = RcloneUploadService(
                recording_config(root),
                upload_config(delete_after_success=False),
            )
            create_process = AsyncMock(side_effect=(FakeProcess(), FakeProcess()))
            bundle = service._bundle(root, audio_path.stem)

            with patch("asyncio.create_subprocess_exec", create_process):
                await service._process_bundle(bundle)
                await service._process_bundle(bundle)

            self.assertEqual(create_process.await_count, 2)
            self.assertTrue(audio_path.exists())
            receipt = json.loads(bundle.receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(receipt["remote"], "dropbox-audio")
            self.assertEqual(
                receipt["files"], [audio_path.name, f"{audio_path.stem}.json"]
            )

    async def test_existing_receipt_resumes_interrupted_local_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio_path, metadata_path = self._write_bundle(root)
            service = RcloneUploadService(
                recording_config(root),
                upload_config(delete_after_success=True),
            )
            bundle = service._bundle(root, audio_path.stem)
            bundle.receipt_path.write_text("{}", encoding="utf-8")

            with patch("asyncio.create_subprocess_exec", AsyncMock()) as create_process:
                await service._process_bundle(bundle)

            create_process.assert_not_awaited()
            self.assertFalse(audio_path.exists())
            self.assertFalse(metadata_path.exists())
            self.assertFalse(bundle.receipt_path.exists())

    async def test_late_transcript_is_uploaded_before_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio_path, _ = self._write_bundle(root)
            service = RcloneUploadService(
                recording_config(root),
                upload_config(
                    delete_after_success=True,
                    wait_for_transcription=True,
                ),
            )
            bundle = service._bundle(root, audio_path.stem)
            create_process = AsyncMock(
                side_effect=(
                    FakeProcess(),
                    FakeProcess(),
                    FakeProcess(),
                    FakeProcess(),
                )
            )

            with patch("asyncio.create_subprocess_exec", create_process):
                await service._process_bundle(bundle)
                self.assertTrue(audio_path.exists())

                bundle.transcript_path.write_text("hello\n", encoding="utf-8")
                bundle.transcript_record_path.write_text("{}", encoding="utf-8")
                await service._process_bundle(bundle)

            self.assertEqual(create_process.await_count, 4)
            uploaded_names = [
                Path(call.args[2]).name for call in create_process.await_args_list
            ]
            self.assertEqual(
                uploaded_names,
                [
                    audio_path.name,
                    bundle.metadata_path.name,
                    bundle.transcript_path.name,
                    bundle.transcript_record_path.name,
                ],
            )
            self.assertFalse(audio_path.exists())
            self.assertFalse(bundle.transcript_path.exists())
            self.assertFalse(bundle.transcript_record_path.exists())

    async def test_uploaded_bundle_is_retained_until_retention_expires(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio_path, metadata_path = self._write_bundle(root)
            service = RcloneUploadService(
                recording_config(root),
                upload_config(
                    delete_after_success=True,
                    local_retention_hours=48,
                ),
            )
            bundle = service._bundle(root, audio_path.stem)
            create_process = AsyncMock(side_effect=(FakeProcess(), FakeProcess()))

            with patch("asyncio.create_subprocess_exec", create_process):
                await service._process_bundle(bundle)

            self.assertTrue(audio_path.exists())
            self.assertTrue(metadata_path.exists())
            self.assertTrue(bundle.receipt_path.exists())

            with patch("src.upload.time.time", return_value=10**12):
                await service._process_bundle(bundle)

            self.assertEqual(create_process.await_count, 2)
            self.assertFalse(audio_path.exists())
            self.assertFalse(metadata_path.exists())
            self.assertFalse(bundle.receipt_path.exists())

    def test_discovery_ignores_partial_and_unsettled_recordings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "active.part.mp3").write_bytes(b"partial")
            (root / "new.mp3").write_bytes(b"new")
            receipt = root / "cleanup.uploaded.json"
            receipt.write_text("{}", encoding="utf-8")
            service = RcloneUploadService(
                recording_config(root),
                upload_config(delete_after_success=True, settle_seconds=60),
            )

            bundles = service._discover_bundles()

            self.assertEqual(
                [bundle.audio_path.name for bundle in bundles], ["cleanup.mp3"]
            )

    @staticmethod
    def _write_bundle(directory: Path) -> tuple[Path, Path]:
        audio_path = directory / "sound-test.mp3"
        metadata_path = directory / "sound-test.json"
        audio_path.write_bytes(b"mp3")
        metadata_path.write_text("{}", encoding="utf-8")
        return audio_path, metadata_path


if __name__ == "__main__":
    unittest.main()
