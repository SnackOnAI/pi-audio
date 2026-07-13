#!/bin/bash

exec ffmpeg \
-f alsa \
-ac 1 \
-ar 16000 \
-i plughw:CARD=Mic,DEV=0 \
-c:a libmp3lame \
-b:a 64k \
-content_type audio/mpeg \
-f mp3 \
icecast://source:changeme@localhost:8000/live
