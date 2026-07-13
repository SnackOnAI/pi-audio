"""Typed configuration models for Pi Audio Stack."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ConfigurationError(ValueError):
    """Raised when application configuration is invalid."""


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name)

    if not isinstance(value, dict):
        raise ConfigurationError(
            f"Configuration section '{name}' is missing or is not a mapping."
        )

    return value


def _require(section: dict[str, Any], key: str, expected_type: type) -> Any:
    if key not in section:
        raise ConfigurationError(f"Missing required configuration key: {key}")

    value = section[key]

    # bool is a subclass of int, so reject it for numeric settings.
    if expected_type is int and isinstance(value, bool):
        raise ConfigurationError(f"Configuration key '{key}' must be an integer.")

    if not isinstance(value, expected_type):
        raise ConfigurationError(
            f"Configuration key '{key}' must be "
            f"{expected_type.__name__}, got {type(value).__name__}."
        )

    return value


def _require_number(section: dict[str, Any], key: str) -> int | float:
    if key not in section:
        raise ConfigurationError(f"Missing required configuration key: {key}")
    value = section[key]
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ConfigurationError(f"Configuration key '{key}' must be a number.")
    return value


@dataclass(frozen=True, slots=True)
class ApplicationConfig:
    name: str
    version: str


@dataclass(frozen=True, slots=True)
class AudioConfig:
    device: str
    sample_rate: int
    channels: int
    sample_width_bytes: int
    chunk_duration_ms: int
    queue_size: int

    @property
    def frames_per_chunk(self) -> int:
        """Return the number of PCM frames in one capture chunk."""
        return self.sample_rate * self.chunk_duration_ms // 1000


@dataclass(frozen=True, slots=True)
class StreamConfig:
    enabled: bool
    encoder: str
    bitrate_kbps: int
    icecast_url: str
    restart_delay_seconds: int


@dataclass(frozen=True, slots=True)
class VadConfig:
    enabled: bool
    engine: str
    aggressiveness: int
    minimum_speech_ms: int


@dataclass(frozen=True, slots=True)
class ActivityConfig:
    enabled: bool
    threshold_dbfs: float
    minimum_active_ms: int
    silence_timeout_ms: int
    pre_buffer_ms: int
    post_buffer_ms: int


@dataclass(frozen=True, slots=True)
class RecordingConfig:
    directory: Path
    format: str
    bitrate_kbps: int
    minimum_duration_ms: int
    maximum_duration_seconds: int
    metadata_enabled: bool


@dataclass(frozen=True, slots=True)
class UploadConfig:
    enabled: bool
    remote: str
    destination: str
    scan_interval_seconds: int
    settle_seconds: int
    operation_timeout_seconds: int
    retry_initial_seconds: int
    retry_max_seconds: int
    delete_after_success: bool


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    level: str
    directory: Path
    filename: str
    max_bytes: int
    backup_count: int
    console: bool
    json: bool

    @property
    def file_path(self) -> Path:
        return self.directory / self.filename


@dataclass(frozen=True, slots=True)
class HealthConfig:
    enabled: bool
    interval_seconds: int
    file: Path


@dataclass(frozen=True, slots=True)
class AppConfig:
    application: ApplicationConfig
    audio: AudioConfig
    stream: StreamConfig
    vad: VadConfig
    activity: ActivityConfig
    recording: RecordingConfig
    upload: UploadConfig
    logging: LoggingConfig
    health: HealthConfig

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppConfig":
        """Build and validate application configuration."""

        application = _section(data, "application")
        audio = _section(data, "audio")
        stream = _section(data, "stream")
        vad = _section(data, "vad")
        activity = _section(data, "activity")
        recording = _section(data, "recording")
        upload = _section(data, "upload")
        logging_config = _section(data, "logging")
        health = _section(data, "health")

        config = cls(
            application=ApplicationConfig(
                name=_require(application, "name", str),
                version=_require(application, "version", str),
            ),
            audio=AudioConfig(
                device=_require(audio, "device", str),
                sample_rate=_require(audio, "sample_rate", int),
                channels=_require(audio, "channels", int),
                sample_width_bytes=_require(audio, "sample_width_bytes", int),
                chunk_duration_ms=_require(audio, "chunk_duration_ms", int),
                queue_size=_require(audio, "queue_size", int),
            ),
            stream=StreamConfig(
                enabled=_require(stream, "enabled", bool),
                encoder=_require(stream, "encoder", str),
                bitrate_kbps=_require(stream, "bitrate_kbps", int),
                icecast_url=_require(stream, "icecast_url", str),
                restart_delay_seconds=_require(stream, "restart_delay_seconds", int),
            ),
            vad=VadConfig(
                enabled=_require(vad, "enabled", bool),
                engine=_require(vad, "engine", str),
                aggressiveness=_require(vad, "aggressiveness", int),
                minimum_speech_ms=_require(vad, "minimum_speech_ms", int),
            ),
            activity=ActivityConfig(
                enabled=_require(activity, "enabled", bool),
                threshold_dbfs=float(_require_number(activity, "threshold_dbfs")),
                minimum_active_ms=_require(activity, "minimum_active_ms", int),
                silence_timeout_ms=_require(activity, "silence_timeout_ms", int),
                pre_buffer_ms=_require(activity, "pre_buffer_ms", int),
                post_buffer_ms=_require(activity, "post_buffer_ms", int),
            ),
            recording=RecordingConfig(
                directory=Path(_require(recording, "directory", str)),
                format=_require(recording, "format", str),
                bitrate_kbps=_require(recording, "bitrate_kbps", int),
                minimum_duration_ms=_require(recording, "minimum_duration_ms", int),
                maximum_duration_seconds=_require(
                    recording, "maximum_duration_seconds", int
                ),
                metadata_enabled=_require(recording, "metadata_enabled", bool),
            ),
            upload=UploadConfig(
                enabled=_require(upload, "enabled", bool),
                remote=_require(upload, "remote", str),
                destination=_require(upload, "destination", str),
                scan_interval_seconds=_require(upload, "scan_interval_seconds", int),
                settle_seconds=_require(upload, "settle_seconds", int),
                operation_timeout_seconds=_require(
                    upload, "operation_timeout_seconds", int
                ),
                retry_initial_seconds=_require(upload, "retry_initial_seconds", int),
                retry_max_seconds=_require(upload, "retry_max_seconds", int),
                delete_after_success=_require(upload, "delete_after_success", bool),
            ),
            logging=LoggingConfig(
                level=_require(logging_config, "level", str).upper(),
                directory=Path(_require(logging_config, "directory", str)),
                filename=_require(logging_config, "filename", str),
                max_bytes=_require(logging_config, "max_bytes", int),
                backup_count=_require(logging_config, "backup_count", int),
                console=_require(logging_config, "console", bool),
                json=_require(logging_config, "json", bool),
            ),
            health=HealthConfig(
                enabled=_require(health, "enabled", bool),
                interval_seconds=_require(health, "interval_seconds", int),
                file=Path(_require(health, "file", str)),
            ),
        )

        config.validate()
        return config

    def validate(self) -> None:
        """Validate cross-field and range constraints."""

        if self.audio.sample_rate <= 0:
            raise ConfigurationError("audio.sample_rate must be positive.")

        if self.audio.channels not in (1, 2):
            raise ConfigurationError("audio.channels must be either 1 or 2.")

        if self.audio.sample_width_bytes not in (1, 2, 3, 4):
            raise ConfigurationError(
                "audio.sample_width_bytes must be between 1 and 4."
            )

        if self.audio.chunk_duration_ms <= 0:
            raise ConfigurationError("audio.chunk_duration_ms must be positive.")

        if self.audio.frames_per_chunk <= 0:
            raise ConfigurationError("Audio settings result in zero frames per chunk.")

        if self.audio.queue_size <= 0:
            raise ConfigurationError("audio.queue_size must be positive.")

        if self.vad.engine.lower() != "webrtc":
            raise ConfigurationError("vad.engine must be 'webrtc'.")

        if self.vad.enabled and (
            self.audio.sample_rate != 16_000
            or self.audio.channels != 1
            or self.audio.sample_width_bytes != 2
            or self.audio.chunk_duration_ms not in (10, 20, 30)
        ):
            raise ConfigurationError(
                "WebRTC VAD requires 16 kHz, mono, signed 16-bit PCM in "
                "10, 20, or 30 ms frames."
            )

        if self.vad.aggressiveness not in (0, 1, 2, 3):
            raise ConfigurationError("vad.aggressiveness must be between 0 and 3.")

        if self.vad.minimum_speech_ms <= 0:
            raise ConfigurationError("vad.minimum_speech_ms must be positive.")

        if not -96.0 <= self.activity.threshold_dbfs <= 0.0:
            raise ConfigurationError(
                "activity.threshold_dbfs must be between -96.0 and 0.0."
            )

        if self.activity.minimum_active_ms <= 0:
            raise ConfigurationError("activity.minimum_active_ms must be positive.")

        if self.activity.silence_timeout_ms <= 0:
            raise ConfigurationError("activity.silence_timeout_ms must be positive.")

        if self.activity.pre_buffer_ms < 0 or self.activity.post_buffer_ms < 0:
            raise ConfigurationError(
                "Activity pre-buffer and post-buffer values cannot be negative."
            )

        if (
            self.activity.pre_buffer_ms
            > self.recording.maximum_duration_seconds * 1_000
        ):
            raise ConfigurationError(
                "activity.pre_buffer_ms cannot exceed the maximum recording duration."
            )

        if self.recording.format.lower() != "mp3":
            raise ConfigurationError(
                "Only MP3 recording is supported in version 0.1.0."
            )

        if self.recording.bitrate_kbps <= 0:
            raise ConfigurationError("recording.bitrate_kbps must be positive.")

        if self.recording.minimum_duration_ms < 0:
            raise ConfigurationError(
                "recording.minimum_duration_ms cannot be negative."
            )

        if self.recording.maximum_duration_seconds <= 0:
            raise ConfigurationError(
                "recording.maximum_duration_seconds must be positive."
            )

        if (
            self.recording.minimum_duration_ms
            > self.recording.maximum_duration_seconds * 1_000
        ):
            raise ConfigurationError(
                "recording.minimum_duration_ms cannot exceed the maximum duration."
            )

        if self.upload.retry_initial_seconds <= 0:
            raise ConfigurationError("upload.retry_initial_seconds must be positive.")

        if not self.upload.remote.strip() or ":" in self.upload.remote:
            raise ConfigurationError(
                "upload.remote must be a non-empty rclone remote name without ':'."
            )

        if not self.upload.destination.strip().strip("/"):
            raise ConfigurationError("upload.destination must not be empty.")

        if self.upload.scan_interval_seconds <= 0:
            raise ConfigurationError("upload.scan_interval_seconds must be positive.")

        if self.upload.settle_seconds < 0:
            raise ConfigurationError("upload.settle_seconds cannot be negative.")

        if self.upload.operation_timeout_seconds <= 0:
            raise ConfigurationError(
                "upload.operation_timeout_seconds must be positive."
            )

        if self.upload.retry_max_seconds < self.upload.retry_initial_seconds:
            raise ConfigurationError(
                "upload.retry_max_seconds must be greater than or equal to "
                "upload.retry_initial_seconds."
            )

        if self.logging.max_bytes <= 0:
            raise ConfigurationError("logging.max_bytes must be positive.")

        if self.logging.backup_count < 1:
            raise ConfigurationError("logging.backup_count must be at least 1.")

        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if self.logging.level not in valid_levels:
            raise ConfigurationError(
                f"logging.level must be one of {sorted(valid_levels)}."
            )
