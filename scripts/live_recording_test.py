"""Create one fixed-duration MP3 through the production recording path."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from src.audio import (
    AlsaAudioSource,
    AudioCaptureService,
    AudioFrame,
    AudioFrameBroker,
    AudioFrameSubscription,
    FfmpegAudioRecorder,
)
from src.config import load_config


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Test MP3 recording through AudioCaptureService"
    )
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--seconds", type=float, default=5.0)
    return parser.parse_args()


async def record(config_path: Path, seconds: float) -> Path:
    config = load_config(config_path)
    if seconds * 1_000 < config.recording.minimum_duration_ms:
        raise ValueError("seconds is below recording.minimum_duration_ms")
    if seconds > config.recording.maximum_duration_seconds:
        raise ValueError("seconds exceeds recording.maximum_duration_seconds")

    broker = AudioFrameBroker(queue_size=config.audio.queue_size)
    source = AlsaAudioSource(
        device=config.audio.device,
        sample_rate=config.audio.sample_rate,
        channels=config.audio.channels,
        sample_width_bytes=config.audio.sample_width_bytes,
        frames_per_chunk=config.audio.frames_per_chunk,
    )
    capture_service = AudioCaptureService(source, broker)
    recorder = FfmpegAudioRecorder(config.audio, config.recording)
    subscription = broker.subscribe()
    frame_target = max(
        1,
        round(seconds * config.audio.sample_rate / config.audio.frames_per_chunk),
    )

    try:
        await recorder.start()
        await capture_service.start()
        for _ in range(frame_target):
            await recorder.write_frame(await next_frame(subscription, capture_service))
        result = await recorder.finish()
        if result is None:
            raise RuntimeError("test recording was unexpectedly discarded")
        return result.path
    except BaseException:
        await recorder.abort()
        raise
    finally:
        subscription.close()
        await capture_service.stop()


async def next_frame(
    subscription: AudioFrameSubscription,
    capture_service: AudioCaptureService,
) -> AudioFrame:
    """Return a frame or immediately surface a capture-process failure."""

    frame_task = asyncio.create_task(subscription.__anext__())
    capture_task = asyncio.create_task(capture_service.wait())
    done, pending = await asyncio.wait(
        {frame_task, capture_task}, return_when=asyncio.FIRST_COMPLETED
    )
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    if frame_task in done:
        return frame_task.result()
    await capture_task
    raise RuntimeError("audio capture stopped without an error or audio frame")


def main() -> int:
    args = parse_arguments()
    print(
        f"Recording {args.seconds:g} seconds from the configured microphone...",
        flush=True,
    )
    path = asyncio.run(record(args.config, args.seconds))
    print(f"MP3 recording captured to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
