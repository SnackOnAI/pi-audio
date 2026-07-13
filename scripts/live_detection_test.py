"""Exercise sound-triggered recording and VAD with the live microphone."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from src.audio import (
    AlsaAudioSource,
    AudioCaptureService,
    AudioFrameBroker,
    FfmpegAudioRecorder,
    RmsAudioActivityDetector,
    SoundRecordingService,
    WebRtcVoiceActivityDetector,
)
from src.config import load_config


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Test sound-triggered recording through AudioCaptureService"
    )
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--seconds", type=float, default=15.0)
    return parser.parse_args()


async def detect(config_path: Path, seconds: float) -> list[Path]:
    if seconds <= 0:
        raise ValueError("seconds must be positive")
    config = load_config(config_path)
    if not config.activity.enabled:
        raise ValueError("activity.enabled must be true for this test")

    recording_directory = config.recording.directory
    before = set(recording_directory.glob("*.mp3"))
    broker = AudioFrameBroker(queue_size=config.audio.queue_size)
    source = AlsaAudioSource(
        device=config.audio.device,
        sample_rate=config.audio.sample_rate,
        channels=config.audio.channels,
        sample_width_bytes=config.audio.sample_width_bytes,
        frames_per_chunk=config.audio.frames_per_chunk,
    )
    capture_service = AudioCaptureService(source, broker)
    detection_service = SoundRecordingService(
        broker,
        RmsAudioActivityDetector(config.activity.threshold_dbfs),
        FfmpegAudioRecorder(config.audio, config.recording),
        config.audio,
        config.activity,
        config.recording,
        voice_detector=(
            WebRtcVoiceActivityDetector(config.audio, config.vad.aggressiveness)
            if config.vad.enabled
            else None
        ),
        vad_config=config.vad if config.vad.enabled else None,
    )

    try:
        await detection_service.start()
        await capture_service.start()
        capture_waiter = asyncio.create_task(capture_service.wait())
        timer = asyncio.create_task(asyncio.sleep(seconds))
        done, pending = await asyncio.wait(
            {capture_waiter, timer}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if capture_waiter in done:
            await capture_waiter
            raise RuntimeError("audio capture stopped unexpectedly")
    finally:
        await capture_service.stop()
        await detection_service.stop()

    after = set(recording_directory.glob("*.mp3"))
    return sorted(after - before)


def main() -> int:
    args = parse_arguments()
    config = load_config(args.config)
    print(
        f"Listening for {args.seconds:g} seconds; make both speech and "
        f"non-speech sounds (threshold {config.activity.threshold_dbfs:g} dBFS)...",
        flush=True,
    )
    paths = asyncio.run(detect(args.config, args.seconds))
    if not paths:
        print("No sound crossed the configured threshold; no recording was made.")
        return 1
    for path in paths:
        print(f"Sound recording captured to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
