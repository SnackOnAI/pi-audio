"""Capture a short WAV through the production Sprint 1 audio pipeline."""

from __future__ import annotations

import argparse
import asyncio
import wave
from pathlib import Path

from src.audio import (
    AlsaAudioSource,
    AudioCaptureService,
    AudioFrame,
    AudioFrameBroker,
    AudioFrameSubscription,
)
from src.config import load_config


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Test an ALSA microphone through AudioCaptureService"
    )
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--seconds", type=float, default=5.0)
    parser.add_argument(
        "--output", type=Path, default=Path("recordings/microphone-test.wav")
    )
    return parser.parse_args()


async def capture(config_path: Path, seconds: float, output: Path) -> None:
    if seconds <= 0:
        raise ValueError("seconds must be positive")

    config = load_config(config_path)
    source = AlsaAudioSource(
        device=config.audio.device,
        sample_rate=config.audio.sample_rate,
        channels=config.audio.channels,
        sample_width_bytes=config.audio.sample_width_bytes,
        frames_per_chunk=config.audio.frames_per_chunk,
    )
    broker = AudioFrameBroker(queue_size=config.audio.queue_size)
    subscription = broker.subscribe()
    service = AudioCaptureService(source, broker)
    frame_target = max(
        1,
        round(seconds * config.audio.sample_rate / config.audio.frames_per_chunk),
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        await service.start()
        with wave.open(str(output), "wb") as wav_file:
            wav_file.setnchannels(config.audio.channels)
            wav_file.setsampwidth(config.audio.sample_width_bytes)
            wav_file.setframerate(config.audio.sample_rate)
            for _ in range(frame_target):
                frame = await next_frame(subscription, service)
                wav_file.writeframes(frame.data)
    finally:
        subscription.close()
        await service.stop()


async def next_frame(
    subscription: AudioFrameSubscription,
    service: AudioCaptureService,
) -> AudioFrame:
    """Return the next frame or immediately surface capture-process failure."""

    frame_task = asyncio.create_task(subscription.__anext__())
    capture_task = asyncio.create_task(service.wait())
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
        f"Capturing {args.seconds:g} seconds from the configured microphone...",
        flush=True,
    )
    asyncio.run(capture(args.config, args.seconds, args.output))
    print(f"Microphone test captured to {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
