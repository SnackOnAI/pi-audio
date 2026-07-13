# Recording privacy control

Local recording creation can be paused and resumed while the single
`audio-stack.service` process remains running.

```bash
recordingstop
recordingstart
recordingcheck
```

`recordingstop` creates a durable marker in the recordings directory. The sound
recording service observes it within one captured audio frame (normally 30 ms),
closes any active MP3 cleanly, clears the rolling pre-buffer, and ignores subsequent
audio for recording purposes. Paused audio is not written into a later recording.

`recordingstart` removes the marker and begins a fresh rolling pre-buffer. The pause
state survives application restarts and reboots.

This control only stops local file creation. Microphone capture and the public
Icecast stream continue. Existing recordings, transcription, Dropbox uploads, and
retention cleanup also continue. Stop or restrict Icecast separately when privacy
requires audio not to leave the room at all.
