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
    AudioCaptureService,
    AudioFrameBroker,
    FfmpegStreamingService,
)
from .config import load_config
from .log_setup import configure_logging
from .models import AppConfig, ConfigurationError


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
    capture_service = AudioCaptureService(source, broker, logger)
    streaming_service = (
        FfmpegStreamingService(broker, config.audio, config.stream, logger=logger)
        if config.stream.enabled
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
        },
    )

    shutdown_waiter: asyncio.Task[bool] | None = None
    capture_waiter: asyncio.Task[None] | None = None
    streaming_waiter: asyncio.Task[None] | None = None
    try:
        if streaming_service is not None:
            await streaming_service.start()
        await capture_service.start()

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
        supervised_tasks = {shutdown_waiter, capture_waiter}
        if streaming_waiter is not None:
            supervised_tasks.add(streaming_waiter)
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
    finally:
        waiters = (shutdown_waiter, capture_waiter, streaming_waiter)
        for waiter in waiters:
            if waiter is not None and not waiter.done():
                waiter.cancel()
        await asyncio.gather(
            *(waiter for waiter in waiters if waiter is not None),
            return_exceptions=True,
        )
        try:
            await capture_service.stop()
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


if __name__ == "__main__":
    raise SystemExit(main())
