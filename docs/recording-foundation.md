# Recording foundation

`FfmpegAudioRecorder` creates explicitly bounded MP3 recordings from immutable
PCM `AudioFrame` objects. It does not subscribe to ALSA or decide when sound
starts and ends. The sound detection service owns those decisions and calls the
minimal `start`, `write_frame`, `finish`, and `abort` lifecycle.

FFmpeg writes each recording to a unique `.part.mp3` path. The recorder exposes
the final `.mp3` only after FFmpeg exits successfully and the configured minimum
duration is satisfied. Aborted, failed, and too-short recordings are removed.
When enabled, a JSON sidecar records timing, duration, PCM frame count, and final
file size.

## Raspberry Pi live test

The 24/7 service owns the microphone, so stop it before this test:

```console
systemctl --user stop audio-stack.service
cd ~/pi-audio
set -a
. ./.env
set +a
.venv/bin/python -m scripts.live_recording_test --seconds 5
```

Copy the reported MP3 to another device or play it through an explicitly chosen
ALSA playback device. The MP3 and matching JSON metadata file are written under
`recordings/`.

Always restore the 24/7 service after testing:

```console
systemctl --user start audio-stack.service
```
