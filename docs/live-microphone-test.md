# Live microphone test

This test captures a short WAV through the production Sprint 1 path:

`arecord` → `AlsaAudioSource` → `AudioCaptureService` → `AudioFrameBroker`

It does not open ALSA through a second component and does not send PCM through
the control EventBus.

## Raspberry Pi setup

Connect the Samson Go Mic, then list its ALSA capture devices:

```console
arecord -l
```

Set `audio.device` in `config.yaml` to the corresponding stable ALSA name. The
current default is `plughw:CARD=Mic,DEV=0`. ALSA card names are preferred over
numeric card indexes because USB enumeration order can change after a reboot.

Install the runtime dependencies and run a five-second test from the repository
root:

```console
sudo apt-get install alsa-utils
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
ICECAST_SOURCE_PASSWORD=test .venv/bin/python -m scripts.live_microphone_test
```

The temporary password is only needed because the single configuration file
also contains the future Icecast URL. The test does not connect to Icecast.

Play back the result:

```console
aplay recordings/microphone-test.wav
```

Use `--seconds`, `--output`, or `--config` to override the defaults. A clean WAV
with the expected duration confirms that the microphone, ALSA configuration,
supervised `arecord` process, capture service, and frame broker work together.
