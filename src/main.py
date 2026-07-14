"""Pi Audio Stack application entry point."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from .audio import (
    AlsaAudioSource,
    AlsaAudioGainControl,
    AudioCaptureService,
    AudioFrameBroker,
    FfmpegAudioRecorder,
    FfmpegStreamingService,
    RmsAudioActivityDetector,
    SoundRecordingService,
    WebRtcVoiceActivityDetector,
)
from .config import load_config
from .control_api import ControlApiService
from .log_setup import configure_logging
from .models import AppConfig, ConfigurationError
from .transcription import (
    FfmpegSpeechScreener,
    OpenAITranscriptionClient,
    RecordingTranscriptionService,
)
from .upload import RcloneUploadService


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pi Audio Stack")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.yaml"),
        help="Path to the YAML configuration file.",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Validate configuration and exit.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()

    try:
        config = load_config(args.config)
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    logger = configure_logging(config.logging)

    logger.info(
        "Configuration loaded",
        extra={
            "event": "configuration_loaded",
            "application": config.application.name,
            "version": config.application.version,
            "config_path": str(args.config.resolve()),
            "audio_device": config.audio.device,
            "sample_rate": config.audio.sample_rate,
            "frames_per_chunk": config.audio.frames_per_chunk,
        },
    )

    if args.check_config:
        logger.info(
            "Configuration validation succeeded",
            extra={"event": "configuration_valid"},
        )
        return 0

    try:
        asyncio.run(run_application(config, logger))
    except KeyboardInterrupt:
        return 130
    except Exception:
        logger.exception(
            "Application stopped unexpectedly",
            extra={"event": "application_failed"},
        )
        return 1
    return 0


async def run_application(config: AppConfig, logger: logging.Logger) -> None:
    """Run the single-process capture and streaming application."""

    broker = AudioFrameBroker(queue_size=config.audio.queue_size)
    source = AlsaAudioSource(
        device=config.audio.device,
        sample_rate=config.audio.sample_rate,
        channels=config.audio.channels,
        sample_width_bytes=config.audio.sample_width_bytes,
        frames_per_chunk=config.audio.frames_per_chunk,
    )
    gain_control = (
        AlsaAudioGainControl(
            device=config.gain.mixer_device,
            control=config.gain.mixer_control,
            operation_timeout_seconds=config.gain.operation_timeout_seconds,
        )
        if config.gain.enabled
        else None
    )
    capture_service = AudioCaptureService(
        source,
        broker,
        logger,
        gain_control=gain_control,
    )
    streaming_service = (
        FfmpegStreamingService(broker, config.audio, config.stream, logger=logger)
        if config.stream.enabled
        else None
    )
    detection_service = (
        SoundRecordingService(
            broker,
            RmsAudioActivityDetector(config.activity.threshold_dbfs),
            FfmpegAudioRecorder(config.audio, config.recording, logger=logger),
            config.audio,
            config.activity,
            config.recording,
            voice_detector=(
                WebRtcVoiceActivityDetector(config.audio, config.vad.aggressiveness)
                if config.vad.enabled
                else None
            ),
            vad_config=config.vad if config.vad.enabled else None,
            logger=logger,
        )
        if config.activity.enabled
        else None
    )
    upload_service = (
        RcloneUploadService(
            config.recording,
            config.upload,
            logger=logger,
        )
        if config.upload.enabled
        else None
    )
    transcription_service = (
        RecordingTranscriptionService(
            config.recording,
            config.transcription,
            FfmpegSpeechScreener(config.audio, config.transcription),
            OpenAITranscriptionClient(config.transcription),
            logger=logger,
        )
        if config.transcription.enabled
        else None
    )

    def runtime_status() -> dict[str, str]:
        return {
            "capture": "running" if capture_service.is_running else "stopped",
            "stream": _service_state(streaming_service),
            "recording": _service_state(detection_service),
            "upload": _service_state(upload_service),
            "transcription": _service_state(transcription_service),
        }

    control_api_service = (
        ControlApiService(
            config.control_api,
            config.recording,
            config.transcription,
            runtime_status,
            gain_getter=(
                capture_service.get_input_gain if config.gain.enabled else None
            ),
            gain_setter=(
                capture_service.set_input_gain if config.gain.enabled else None
            ),
            logger=logger,
        )
        if config.control_api.enabled
        else None
    )
    shutdown_requested = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed_signals: list[signal.Signals] = []

    for handled_signal in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(handled_signal, shutdown_requested.set)
            installed_signals.append(handled_signal)
        except NotImplementedError:
            pass

    logger.info(
        "Application starting",
        extra={
            "event": "application_starting",
            "stream_enabled": config.stream.enabled,
            "activity_recording_enabled": config.activity.enabled,
            "vad_enabled": config.vad.enabled,
            "upload_enabled": config.upload.enabled,
            "transcription_enabled": config.transcription.enabled,
            "transcription_model": config.transcription.model,
            "control_api_enabled": config.control_api.enabled,
            "gain_control_enabled": config.gain.enabled,
        },
    )

    shutdown_waiter: asyncio.Task[bool] | None = None
    capture_waiter: asyncio.Task[None] | None = None
    streaming_waiter: asyncio.Task[None] | None = None
    detection_waiter: asyncio.Task[None] | None = None
    upload_waiter: asyncio.Task[None] | None = None
    transcription_waiter: asyncio.Task[None] | None = None
    control_api_waiter: asyncio.Task[None] | None = None
    try:
        if transcription_service is not None:
            await transcription_service.start()
        if upload_service is not None:
            await upload_service.start()
        if detection_service is not None:
            await detection_service.start()
        if streaming_service is not None:
            await streaming_service.start()
        await capture_service.start()
        if control_api_service is not None:
            await control_api_service.start()

        shutdown_waiter = asyncio.create_task(
            shutdown_requested.wait(), name="shutdown-signal"
        )
        capture_waiter = asyncio.create_task(
            capture_service.wait(), name="capture-supervisor"
        )
        if streaming_service is not None:
            streaming_waiter = asyncio.create_task(
                streaming_service.wait(), name="stream-supervisor"
            )
        if detection_service is not None:
            detection_waiter = asyncio.create_task(
                detection_service.wait(), name="detection-supervisor"
            )
        if upload_service is not None:
            upload_waiter = asyncio.create_task(
                upload_service.wait(), name="upload-supervisor"
            )
        if transcription_service is not None:
            transcription_waiter = asyncio.create_task(
                transcription_service.wait(), name="transcription-supervisor"
            )
        if control_api_service is not None:
            control_api_waiter = asyncio.create_task(
                control_api_service.wait(), name="control-api-supervisor"
            )
        supervised_tasks = {shutdown_waiter, capture_waiter}
        if streaming_waiter is not None:
            supervised_tasks.add(streaming_waiter)
        if detection_waiter is not None:
            supervised_tasks.add(detection_waiter)
        if upload_waiter is not None:
            supervised_tasks.add(upload_waiter)
        if transcription_waiter is not None:
            supervised_tasks.add(transcription_waiter)
        if control_api_waiter is not None:
            supervised_tasks.add(control_api_waiter)
        done, _ = await asyncio.wait(
            supervised_tasks,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if capture_waiter in done:
            await capture_waiter
            raise RuntimeError("audio capture stopped unexpectedly")
        if streaming_waiter is not None and streaming_waiter in done:
            await streaming_waiter
            raise RuntimeError("audio streaming stopped unexpectedly")
        if detection_waiter is not None and detection_waiter in done:
            await detection_waiter
            raise RuntimeError("audio detection stopped unexpectedly")
        if upload_waiter is not None and upload_waiter in done:
            await upload_waiter
            raise RuntimeError("recording upload stopped unexpectedly")
        if transcription_waiter is not None and transcription_waiter in done:
            await transcription_waiter
            raise RuntimeError("recording transcription stopped unexpectedly")
        if control_api_waiter is not None and control_api_waiter in done:
            await control_api_waiter
            raise RuntimeError("control API stopped unexpectedly")
    finally:
        waiters = (
            shutdown_waiter,
            capture_waiter,
            streaming_waiter,
            detection_waiter,
            upload_waiter,
            transcription_waiter,
            control_api_waiter,
        )
        for waiter in waiters:
            if waiter is not None and not waiter.done():
                waiter.cancel()
        await asyncio.gather(
            *(waiter for waiter in waiters if waiter is not None),
            return_exceptions=True,
        )
        try:
            if control_api_service is not None:
                await control_api_service.stop()
        finally:
            try:
                await capture_service.stop()
            finally:
                try:
                    if detection_service is not None:
                        await detection_service.stop()
                finally:
                    try:
                        if transcription_service is not None:
                            await transcription_service.stop()
                    finally:
                        try:
                            if upload_service is not None:
                                await upload_service.stop()
                        finally:
                            try:
                                if streaming_service is not None:
                                    await streaming_service.stop()
                            finally:
                                for handled_signal in installed_signals:
                                    loop.remove_signal_handler(handled_signal)
                                logger.info(
                                    "Application stopped",
                                    extra={"event": "application_stopped"},
                                )


def _service_state(service: object | None) -> str:
    if service is None:
        return "disabled"
    return "running" if bool(getattr(service, "is_running", False)) else "stopped"


if __name__ == "__main__":
    raise SystemExit(main())
