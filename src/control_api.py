"""Authenticated local control API for Home Assistant."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from aiohttp import ContentTypeError, web

from .audio import AudioGain, AudioGainError
from .models import ControlApiConfig, RecordingConfig, TranscriptionConfig
from .recording_control import recording_is_paused, set_recording_paused
from .transcription_control import (
    set_transcription_paused,
    transcription_is_paused,
)


class ControlApiError(RuntimeError):
    """Raised when the local control API cannot start safely."""


StatusProvider = Callable[[], Mapping[str, str]]
GainGetter = Callable[[], Awaitable[AudioGain]]
GainSetter = Callable[[int], Awaitable[AudioGain]]


class ControlApiService:
    """Serve authenticated JSON controls without carrying any PCM audio."""

    def __init__(
        self,
        config: ControlApiConfig,
        recording_config: RecordingConfig,
        transcription_config: TranscriptionConfig,
        status_provider: StatusProvider,
        *,
        gain_getter: GainGetter | None = None,
        gain_setter: GainSetter | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        token = os.environ.get(config.token_environment)
        if not token:
            raise ControlApiError(
                f"required control API token environment variable "
                f"'{config.token_environment}' is not set"
            )
        self._config = config
        self._recording_config = recording_config
        self._transcription_config = transcription_config
        self._status_provider = status_provider
        self._gain_getter = gain_getter
        self._gain_setter = gain_setter
        self._token = token
        self._logger = logger or logging.getLogger("audio_stack.control_api")
        self._runner: web.AppRunner | None = None
        self._stopped = asyncio.Event()

    @property
    def is_running(self) -> bool:
        return self._runner is not None and not self._stopped.is_set()

    async def start(self) -> None:
        if self.is_running:
            raise RuntimeError("control API service is already running")
        self._stopped.clear()
        application = self._create_application()

        runner = web.AppRunner(application, access_log=None)
        try:
            await runner.setup()
            site = web.TCPSite(runner, self._config.host, self._config.port)
            await site.start()
        except BaseException:
            await runner.cleanup()
            self._stopped.set()
            raise
        self._runner = runner
        self._logger.info(
            "Control API started",
            extra={
                "event": "control_api_started",
                "host": self._config.host,
                "port": self._config.port,
            },
        )

    def _create_application(self) -> web.Application:
        application = web.Application(
            middlewares=[self._authenticate],
            client_max_size=1_024,
        )
        application.router.add_get("/api/v1/status", self._get_status)
        application.router.add_post("/api/v1/recording/start", self._start_recording)
        application.router.add_post("/api/v1/recording/stop", self._stop_recording)
        application.router.add_post(
            "/api/v1/transcription/start", self._start_transcription
        )
        application.router.add_post(
            "/api/v1/transcription/stop", self._stop_transcription
        )
        application.router.add_post("/api/v1/gain", self._set_gain)
        return application

    async def stop(self) -> None:
        runner = self._runner
        self._runner = None
        if runner is None:
            self._stopped.set()
            return
        try:
            await runner.cleanup()
        finally:
            self._stopped.set()
            self._logger.info(
                "Control API stopped", extra={"event": "control_api_stopped"}
            )

    async def wait(self) -> None:
        await self._stopped.wait()

    @web.middleware
    async def _authenticate(
        self,
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        supplied = request.headers.get("Authorization", "")
        expected = f"Bearer {self._token}"
        if not hmac.compare_digest(supplied, expected):
            return self._json_response(
                {"error": "unauthorized"},
                status=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await handler(request)

    async def _get_status(self, request: web.Request) -> web.Response:
        del request
        return self._json_response(await self._status_payload())

    async def _start_recording(self, request: web.Request) -> web.Response:
        del request
        set_recording_paused(self._recording_config.directory, False)
        self._log_control("recording", "running")
        return self._json_response(await self._status_payload())

    async def _stop_recording(self, request: web.Request) -> web.Response:
        del request
        set_recording_paused(self._recording_config.directory, True)
        self._log_control("recording", "paused")
        return self._json_response(await self._status_payload())

    async def _start_transcription(self, request: web.Request) -> web.Response:
        del request
        set_transcription_paused(self._recording_config.directory, False)
        self._log_control("transcription", "running")
        return self._json_response(await self._status_payload())

    async def _stop_transcription(self, request: web.Request) -> web.Response:
        del request
        set_transcription_paused(self._recording_config.directory, True)
        self._log_control("transcription", "paused")
        return self._json_response(await self._status_payload())

    async def _set_gain(self, request: web.Request) -> web.Response:
        if self._gain_setter is None:
            return self._json_response(
                {"error": "microphone gain control is disabled"}, status=503
            )
        try:
            payload = await request.json()
        except (ContentTypeError, json.JSONDecodeError, UnicodeDecodeError):
            return self._json_response({"error": "invalid JSON body"}, status=400)
        if not isinstance(payload, dict):
            return self._json_response(
                {"error": "JSON body must be an object"}, status=400
            )
        percent = payload.get("percent")
        if isinstance(percent, bool) or not isinstance(percent, int):
            return self._json_response(
                {"error": "percent must be an integer"}, status=400
            )
        if not 0 <= percent <= 100:
            return self._json_response(
                {"error": "percent must be between 0 and 100"}, status=400
            )
        try:
            gain = await self._gain_setter(percent)
        except AudioGainError as exc:
            self._logger.warning(
                "Microphone gain change failed",
                extra={"event": "microphone_gain_failed", "error": str(exc)},
            )
            return self._json_response(
                {"error": "microphone gain is unavailable"}, status=503
            )
        return self._json_response({"gain": self._gain_payload(gain)})

    async def _status_payload(self) -> dict[str, Any]:
        runtime = dict(self._status_provider())
        payload: dict[str, Any] = {
            "service": "running",
            "capture": runtime.get("capture", "unknown"),
            "stream": runtime.get("stream", "disabled"),
            "recording": self._controlled_state(
                runtime.get("recording", "disabled"),
                recording_is_paused(self._recording_config.directory),
            ),
            "transcription": self._controlled_state(
                runtime.get("transcription", "disabled"),
                transcription_is_paused(self._recording_config.directory),
            ),
            "upload": runtime.get("upload", "disabled"),
            "gain": await self._read_gain(),
            "last_recording": self._last_recording(),
            "transcription_minutes_this_month": self._transcription_minutes(),
            "transcription_monthly_limit_minutes": (
                self._transcription_config.max_monthly_audio_minutes
            ),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return payload

    async def _read_gain(self) -> dict[str, Any]:
        if self._gain_getter is None:
            return {"available": False}
        try:
            return self._gain_payload(await self._gain_getter())
        except AudioGainError as exc:
            self._logger.warning(
                "Microphone gain read failed",
                extra={"event": "microphone_gain_read_failed", "error": str(exc)},
            )
            return {"available": False}

    def _last_recording(self) -> dict[str, Any] | None:
        paths = list(self._recording_config.directory.glob("sound-*.mp3"))
        paths.extend(self._recording_config.directory.glob("speech-*.mp3"))
        try:
            path = max(paths, key=lambda item: item.stat().st_mtime)
            modified_at = datetime.fromtimestamp(
                path.stat().st_mtime, timezone.utc
            ).isoformat()
        except (ValueError, FileNotFoundError, OSError):
            return None
        return {
            "file": path.name,
            "classification": path.name.split("-", 1)[0],
            "modified_at": modified_at,
        }

    def _transcription_minutes(self) -> float:
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        path = self._recording_config.directory / f".transcription-usage-{month}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return round(float(data["audio_seconds"]) / 60, 3)
        except (
            FileNotFoundError,
            OSError,
            ValueError,
            TypeError,
            KeyError,
            json.JSONDecodeError,
        ):
            return 0.0

    def _log_control(self, feature: str, state: str) -> None:
        self._logger.info(
            "Runtime control changed",
            extra={
                "event": "runtime_control_changed",
                "feature": feature,
                "state": state,
            },
        )

    @staticmethod
    def _controlled_state(runtime_state: str, paused: bool) -> str:
        if runtime_state == "disabled":
            return "disabled"
        return "paused" if paused else runtime_state

    @staticmethod
    def _gain_payload(gain: AudioGain) -> dict[str, Any]:
        return {
            "available": True,
            "percent": gain.percent,
            "decibels": gain.decibels,
        }

    @staticmethod
    def _json_response(
        payload: Mapping[str, Any],
        *,
        status: int = 200,
        headers: Mapping[str, str] | None = None,
    ) -> web.Response:
        response_headers = {"Cache-Control": "no-store"}
        if headers is not None:
            response_headers.update(headers)
        return web.json_response(
            payload,
            status=status,
            headers=response_headers,
        )
