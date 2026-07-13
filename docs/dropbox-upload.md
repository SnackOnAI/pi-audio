# Dropbox recording uploads

`RcloneUploadService` runs inside the single asyncio application and supervises
one `rclone copyto` process at a time. It scans the configured recording
directory for finalized MP3 files; PCM audio never enters the uploader or the
control-event path.

Each recording is treated as a bundle containing the MP3 and its JSON metadata
when available. Files ending in `.part.mp3` are ignored. A short settling delay
prevents the uploader racing metadata creation immediately after FFmpeg exposes
the final MP3.

After every available file succeeds, the service writes an atomic
`.uploaded.json` receipt. This receipt makes recovery crash-safe:

- With `delete_after_success: true`, local audio and metadata remain available
  for `local_retention_hours` after confirmed upload. They and the receipt are
  then removed. If cleanup is interrupted, the receipt resumes cleanup without
  uploading the recording again.
- With `delete_after_success: false`, the receipt remains beside the recording
  and prevents duplicate uploads after scans or restarts.
- Failed uploads retain every local recording file and retry with exponential
  backoff up to `retry_max_seconds`.

## Raspberry Pi configuration

The configured rclone remote must exist for the same user that runs
`audio-stack.service`:

```console
rclone listremotes
rclone lsd dropbox-audio:
```

The supplied configuration uploads new bundles into UTC date folders such as
`dropbox-audio:Pi Audio/2026/07/13` and retains a 48-hour local copy. Audio,
recording metadata, transcript text, and transcript metadata from the same bundle
share the same folder. Existing files are not moved. Set `date_subdirectories` to
`false` to restore flat uploads. The scan, settling, operation timeout, retention,
and retry values are independently configurable.
Structured logs use `upload_completed`, `upload_failed`, and
`upload_local_deleted` events.

Because local deletion is enabled by default, confirm a recording is visible in
Dropbox before relying on automatic cleanup in production.
