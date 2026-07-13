# Speech transcription

The transcription worker runs inside the existing `audio-stack.service` process. It
does not access ALSA and it does not put PCM on the EventBus.

## Processing flow

1. The sound recorder atomically finishes an MP3.
2. FFmpeg decodes the MP3 to 16 kHz mono PCM.
3. WebRTC VAD checks for at least 300 ms of continuous speech locally.
4. Files without speech receive a durable `.transcript.json` record and are not sent
   to an external API.
5. Speech-positive files are sent to `gpt-4o-transcribe` with English and microphone
   context supplied to improve recognition.
6. A human-readable `.txt` file and machine-readable `.transcript.json` record are
   written atomically.
7. The uploader detects the new sidecars and adds them to the existing Dropbox
   bundle. Local audio is not deleted until transcription has completed or the file
   has been classified as no speech.

Recordings longer than 10 minutes or larger than 24 MiB are split into 10-minute MP3
chunks before upload to the transcription API. This keeps responses below the
model's output limit. The chunks are temporary and are removed after the request.

If the cloud model finds no intelligible words in a VAD-positive recording, the
worker commits a `no_transcript` record without a `.txt` file. This is a completed
outcome and is not retried, preventing repeated charges for the same noise.

## API key

Create a project API key in the OpenAI Platform. On the Pi, edit the service
environment file without putting the secret in `config.yaml` or Git:

```bash
cd ~/pi-audio
nano .env
```

Keep the existing Icecast line and add:

```text
OPENAI_API_KEY=your-project-api-key
```

Then protect the file and restart the service:

```bash
chmod 600 .env
systemctl --user daemon-reload
systemctl --user restart audio-stack.service
systemctl --user status audio-stack.service --no-pager
```

Audio classified as speech is sent to OpenAI for processing. Do not enable the
feature where sending recorded speech to a cloud provider is inappropriate.

## Accuracy and spend controls

`gpt-4o-transcribe` is the accuracy-first default. To halve the model's published
audio-token rates, change `transcription.model` to
`gpt-4o-mini-transcribe`; accuracy may be lower on difficult room audio.

`max_monthly_audio_minutes` is a hard local submission limit, not a currency
estimate. The default is 1,500 speech-positive recording minutes per UTC calendar
month. Completed usage is stored in a hidden monthly ledger in `recordings/`.
When the limit is reached, untranscribed audio remains local and is retried in the
next UTC month.

Check current model pricing in the OpenAI pricing documentation before changing the
limit. Billing is token-based and therefore varies with both audio and transcript
length.

## Pause or start API usage

These commands take effect without restarting `audio-stack.service`. Recording,
Icecast streaming, sound detection, local VAD, and Dropbox uploads continue normally.

```bash
cd ~/pi-audio

# Prevent new paid transcription requests
.venv/bin/python -m scripts.transcription_control pause

# Check the current state
.venv/bin/python -m scripts.transcription_control status

# Allow transcription requests again
.venv/bin/python -m scripts.transcription_control start
```

An API request already in flight is allowed to finish. While paused, finalized audio
remains available and is processed after transcription is started again.

## Verification

After speaking near the microphone and allowing the recording to finish:

```bash
cd ~/pi-audio
find recordings -name '*.transcript.json' -o -name '*.txt' | tail
tail -n 50 logs/audio-stack.log
```

Successful speech transcription logs `transcription_completed`. Non-speech sound
logs `transcription_no_speech`. API and network errors log `transcription_failed`
and are retried with bounded exponential backoff.
