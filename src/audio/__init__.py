"""Audio capture and distribution primitives."""

from .broker import (
    AudioFrameBroker,
    AudioFrameBrokerFull,
    AudioFrameSubscription,
)
from .capture import AudioCaptureService
from .detection import (
    ActivityDecision,
    AudioActivityDetector,
    AudioDetectionError,
    RmsAudioActivityDetector,
    SoundRecordingService,
    VoiceActivityDetector,
    VoiceDecision,
    WebRtcVoiceActivityDetector,
)
from .models import AudioFrame
from .recording import (
    AudioRecorder,
    AudioRecordingError,
    FfmpegAudioRecorder,
    RecordingResult,
    RecordingSession,
)
from .source import AlsaAudioSource, AudioSource, AudioSourceError
from .stream import AudioStreamingError, FfmpegStreamingService

__all__ = [
    "AlsaAudioSource",
    "ActivityDecision",
    "AudioActivityDetector",
    "AudioCaptureService",
    "AudioDetectionError",
    "AudioFrame",
    "AudioFrameBroker",
    "AudioFrameBrokerFull",
    "AudioFrameSubscription",
    "AudioRecorder",
    "AudioRecordingError",
    "AudioSource",
    "AudioSourceError",
    "AudioStreamingError",
    "FfmpegStreamingService",
    "FfmpegAudioRecorder",
    "RecordingResult",
    "RecordingSession",
    "RmsAudioActivityDetector",
    "SoundRecordingService",
    "VoiceActivityDetector",
    "VoiceDecision",
    "WebRtcVoiceActivityDetector",
]
