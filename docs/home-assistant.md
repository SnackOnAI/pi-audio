# Home Assistant integration

The audio stack exposes a small authenticated JSON API on loopback. Home
Assistant uses that API for status and control; PCM audio continues to flow
only through `AudioFrameBroker` and Icecast.

## Controls and status

- Recording creation can be started or paused independently.
- Paid transcription can be started or paused independently.
- The Samson Go Mic capture gain can be read and set from 0 to 100 percent.
- Capture, Icecast streaming, upload, the last recording classification, and
  monthly transcription minutes are reported as status.

Pausing recording creation does **not** stop the live Icecast stream. Pausing
transcription does **not** stop recording or uploading.

## API security

The API listens on `127.0.0.1:8765` by default. This works when Home Assistant
Container uses host networking and prevents other LAN or Tailnet devices from
calling the control API directly.

Generate a dedicated random token and put it in the audio stack `.env`:

```console
HOME_ASSISTANT_TOKEN=<random-token>
```

Put the same token, prefixed by `Bearer `, in Home Assistant's
`/config/secrets.yaml`:

```yaml
pi_audio_authorization: "Bearer <random-token>"
```

Do not reuse the Icecast source password or an OpenAI key for this token.

## Home Assistant package

The ready-to-use package is
[`homeassistant/pi_audio_package.yaml`](../homeassistant/pi_audio_package.yaml).
Enable packages in Home Assistant's `configuration.yaml` if they are not
already enabled:

```yaml
homeassistant:
  packages: !include_dir_named packages
```

Copy the package to `/config/packages/pi_audio.yaml`, check the Home Assistant
configuration, then restart Home Assistant. It creates:

- `sensor.pi_audio_stack`
- `switch.pi_audio_recording`
- `switch.pi_audio_transcription`
- `number.pi_audio_microphone_gain`
- stream, upload, transcription-minute, and last-classification sensors

An optional ready-to-use sidebar dashboard is provided at
[`homeassistant/pi_audio_dashboard.yaml`](../homeassistant/pi_audio_dashboard.yaml).
Register it under `lovelace.dashboards` and copy it to
`/config/pi_audio_dashboard.yaml`. The gain number renders as a slider in its
entities card.

```yaml
lovelace:
  dashboards:
    pi-audio:
      mode: yaml
      title: Pi Audio
      icon: mdi:microphone
      show_in_sidebar: true
      require_admin: true
      filename: pi_audio_dashboard.yaml
```

The status sensor polls every 10 seconds. A control updates it immediately
after the API call.

## Live stream

Home Assistant can send the existing stream to a media player using:

```yaml
action: media_player.play_media
target:
  entity_id: media_player.your_speaker
data:
  media_content_id: http://127.0.0.1:8000/live.mp3
  media_content_type: audio/mpeg
```

Use `http://100.100.152.118:8000/live.mp3` only when a Tailnet device needs to
fetch the stream directly. Keep the control API bound to loopback.

## Direct API check

Run this on the Pi, substituting the token from `.env` without sharing it:

```console
curl -sS -H "Authorization: Bearer <random-token>" \
  http://127.0.0.1:8765/api/v1/status
```

An absent or incorrect token returns HTTP 401. Gain changes outside 0–100
percent return HTTP 400.
