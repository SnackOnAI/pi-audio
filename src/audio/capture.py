"""Lifecycle service connecting one audio source to the frame broker."""

from __future__ import annotations

import asyncio
import logging

from .broker import AudioFrameBroker
from .gain import AudioGain, AudioGainControl, AudioGainError
from .source import AudioSource, AudioSourceError


class AudioCaptureService:
    """Continuously capture PCM and publish it without using the EventBus."""

    def __init__(
        self,
        source: AudioSource,
        broker: AudioFrameBroker,
        logger: logging.Logger | None = None,
        *,
        gain_control: AudioGainControl | None = None,
    ) -> None:
        self._source = source
        self._broker = broker
        self._gain_control = gain_control
        self._logger = logger or logging.getLogger("audio_stack.capture")
        self._stop_requested = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.is_running:
            raise RuntimeError("audio capture service is already running")
        self._stop_requested.clear()
        await self._source.start()
        self._task = asyncio.create_task(self._run(), name="audio-capture")

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        self._stop_requested.set()
        await self._source.close()
        try:
            await task
        finally:
            self._task = None

    async def wait(self) -> None:
        if self._task is not None:
            await asyncio.shield(self._task)

    async def get_input_gain(self) -> AudioGain:
        """Read microphone gain through the capture service's ALSA gateway."""
        if self._gain_control is None:
            raise AudioGainError("microphone gain control is disabled")
        return await self._gain_control.get()

    async def set_input_gain(self, percent: int) -> AudioGain:
        """Change microphone gain through the capture service's ALSA gateway."""
        if self._gain_control is None:
            raise AudioGainError("microphone gain control is disabled")
        gain = await self._gain_control.set(percent)
        self._logger.info(
            "Microphone gain changed",
            extra={
                "event": "microphone_gain_changed",
                "gain_percent": gain.percent,
                "gain_decibels": gain.decibels,
            },
        )
        return gain

    async def _run(self) -> None:
        self._logger.info("Audio capture started", extra={"event": "capture_started"})
        try:
            while not self._stop_requested.is_set():
                try:
                    frame = await self._source.read_frame()
                except AudioSourceError:
                    if self._stop_requested.is_set():
                        break
                    raise
                self._broker.publish(frame)
        finally:
            await self._source.close()
            self._logger.info(
                "Audio capture stopped", extra={"event": "capture_stopped"}
            )
