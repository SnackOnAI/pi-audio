"""In-process fan-out for PCM audio frames."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager

from .models import AudioFrame


class AudioFrameBrokerFull(RuntimeError):
    """Raised instead of silently losing PCM when a consumer falls behind."""


class AudioFrameSubscription(
    AsyncIterator[AudioFrame], AbstractAsyncContextManager["AudioFrameSubscription"]
):
    """One consumer's private, bounded audio queue."""

    def __init__(
        self, broker: "AudioFrameBroker", subscription_id: int, queue_size: int
    ) -> None:
        self._broker = broker
        self._subscription_id = subscription_id
        self._queue: asyncio.Queue[AudioFrame] = asyncio.Queue(queue_size)
        self._closed = False

    def __aiter__(self) -> "AudioFrameSubscription":
        return self

    async def __anext__(self) -> AudioFrame:
        if self._closed:
            raise StopAsyncIteration
        return await self._queue.get()

    async def __aenter__(self) -> "AudioFrameSubscription":
        return self

    async def __aexit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._broker.unsubscribe(self)

    def _publish(self, frame: AudioFrame) -> None:
        try:
            self._queue.put_nowait(frame)
        except asyncio.QueueFull as exc:
            raise AudioFrameBrokerFull(
                f"audio subscriber {self._subscription_id} queue is full"
            ) from exc


class AudioFrameBroker:
    """Distribute each PCM frame to every active in-process subscriber."""

    def __init__(self, queue_size: int) -> None:
        if queue_size <= 0:
            raise ValueError("queue_size must be positive")
        self._queue_size = queue_size
        self._next_subscription_id = 0
        self._subscriptions: dict[int, AudioFrameSubscription] = {}

    @property
    def subscriber_count(self) -> int:
        return len(self._subscriptions)

    def subscribe(self) -> AudioFrameSubscription:
        subscription_id = self._next_subscription_id
        self._next_subscription_id += 1
        subscription = AudioFrameSubscription(
            self, subscription_id, self._queue_size
        )
        self._subscriptions[subscription_id] = subscription
        return subscription

    def unsubscribe(self, subscription: AudioFrameSubscription) -> None:
        self._subscriptions.pop(subscription._subscription_id, None)

    def publish(self, frame: AudioFrame) -> None:
        subscriptions = tuple(self._subscriptions.values())
        full_subscription = next(
            (
                subscription
                for subscription in subscriptions
                if subscription._queue.full()
            ),
            None,
        )
        if full_subscription is not None:
            raise AudioFrameBrokerFull(
                "audio subscriber "
                f"{full_subscription._subscription_id} queue is full"
            )

        for subscription in subscriptions:
            subscription._publish(frame)
