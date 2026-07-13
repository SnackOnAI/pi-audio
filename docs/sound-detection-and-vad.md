# Sound detection and VAD

Recording and speech classification are intentionally separate:

- RMS sound detection starts recordings for any sufficiently loud audio. This
  includes speech, alarms, impacts, animals, music, and other non-speech noise.
- WebRTC VAD classifies speech for structured log events only. It does not start
  or stop recordings. Those events can feed transcription and AI features later.

Both components consume immutable PCM frames from `AudioFrameBroker`.
`AudioCaptureService` remains the only service that accesses ALSA.

## Configuration

`activity.threshold_dbfs` controls sound sensitivity. A more negative value is
more sensitive: `-40` records quieter sounds than `-15`. The supplied `-15`
setting was tuned against the project's Samson Go Mic and room noise, but should
still be tuned for its room, input gain, and the quietest sound that matters.

`activity.minimum_active_ms` rejects isolated spikes. `pre_buffer_ms` retains
audio before the trigger, while `silence_timeout_ms` plus `post_buffer_ms`
controls how long recording continues after the level falls below the threshold.
Long events are split at `recording.maximum_duration_seconds` without losing the
frame at the boundary.

`vad.aggressiveness` accepts values from 0 to 3. Higher values are stricter about
what WebRTC considers speech. `minimum_speech_ms` prevents a single classified
frame from emitting a `speech_started` event.

## Raspberry Pi live test

The 24/7 service owns the microphone, so stop it before the test. Then make some
speech sounds and a clearly non-speech sound during the listening window:

```console
systemctl --user stop audio-stack.service
cd ~/pi-audio
set -a
. ./.env
set +a
.venv/bin/python -m scripts.live_detection_test --seconds 15
```

The test reports each new MP3 under `recordings/`. JSON logs use separate
`sound_started`, `sound_ended`, `speech_started`, and `speech_ended` events.

Always restore the 24/7 service afterward:

```console
systemctl --user start audio-stack.service
```
