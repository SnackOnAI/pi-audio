# Icecast streaming

The Python application owns the only `arecord` process. PCM frames travel from
`AudioCaptureService` through `AudioFrameBroker` to an FFmpeg process supervised
by `FfmpegStreamingService`. FFmpeg reads raw PCM from standard input; it never
opens ALSA itself.

If FFmpeg or its Icecast connection fails, the streaming service closes that
broker subscription, waits `stream.restart_delay_seconds`, and reconnects with
live audio. It does not replay stale queued frames.

## Raspberry Pi prerequisites

Install FFmpeg and confirm that native Icecast is running:

```console
sudo apt update
sudo apt install -y ffmpeg
ffmpeg -version
systemctl is-active icecast2
```

Create a private environment file in the repository. This file is ignored by
Git:

```console
cd ~/pi-audio
umask 077
printf 'ICECAST_SOURCE_PASSWORD=changeme\n' > .env
chmod 600 .env
```

The password must match `<source-password>` in
`/etc/icecast2/icecast.xml`. Replace the temporary password before exposing the
server outside the trusted local network.

## Manual live test

Load the environment and run the complete application:

```console
cd ~/pi-audio
set -a
. ./.env
set +a
.venv/bin/python -m src.main --config config.yaml
```

From another device on the local network, open:

```text
http://192.168.1.35:8000/live.mp3
```

Press `Ctrl+C` once to stop the manual test. Both `arecord` and FFmpeg should
exit cleanly.

## 24/7 user service

Install the supplied unit for the current Raspberry Pi user:

```console
mkdir -p ~/.config/systemd/user
cp systemd/audio-stack.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now audio-stack.service
sudo loginctl enable-linger "$USER"
```

Linger starts the user service during boot without requiring an interactive
login. Inspect its status and structured logs with:

```console
systemctl --user status audio-stack.service
journalctl --user -u audio-stack.service -f
```

Restart after configuration changes with:

```console
systemctl --user restart audio-stack.service
```
