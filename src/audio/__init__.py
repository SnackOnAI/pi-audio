"""Audio capture and distribution primitives."""

from .broker import (
    AudioFrameBroker,
    AudioFrameBrokerFull,
    AudioFrameSubscription,
)
from .capture import AudioCaptureService
from .gain import (
    AlsaAudioGainControl,
    AudioGain,
    AudioGainControl,
    AudioGainError,
)
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
    "AlsaAudioGainControl",
    "ActivityDecision",
    "AudioActivityDetector",
    "AudioCaptureService",
    "AudioDetectionError",
    "AudioFrame",
    "AudioFrameBroker",
    "AudioFrameBrokerFull",
    "AudioFrameSubscription",
    "AudioGain",
    "AudioGainControl",
    "AudioGainError",
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
