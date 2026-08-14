# Metadata Cleaner

Owner-only web utility for removing embedded metadata from supported images and videos. The source file is never changed. Outputs are one-use downloads and expire automatically.

## What it does

- JPEG, PNG and WebP: applies EXIF orientation, rebuilds the output from decoded pixels, and does not copy inherited metadata.
- MP4, MOV, M4V, WebM and MKV: copies only normal video and audio streams to a new container without re-encoding.
- Drops container and stream tags classified as removable, chapters, subtitles, data tracks, attachments and attached cover art.
- Probes the output and rejects it if a removable field, chapter or non-audio/video track remains.
- Shows a bounded per-file report of fields and tracks found, removed and retained.

Structural fields required for a valid playable file may remain. The tool does not remove information from visible pixels, audio, steganography, sidecar files, filesystem records, cloud-provider records or copies already sent elsewhere.

## Security model

This app is designed for a single trusted owner behind a loopback reverse proxy such as Tailscale Serve. Do not expose the backend directly to a LAN or the public internet.

- Backend binds to loopback by default.
- Remote requests require the configured proxy host and exact identity header.
- State-changing requests require a matching Origin and an in-memory CSRF token.
- Uploads have byte, pixel, concurrency and free-disk limits.
- Temporary files use owner-only permissions.
- Download URLs are random, authenticated, one-use and time-limited.
- Filenames and metadata values are not written to application logs.
- Browser responses disable caching and use a restrictive Content Security Policy.

The media parsers process untrusted files. Keep Python dependencies, Pillow, FFmpeg and ExifTool patched. This project does not claim to remove steganography or every possible codec-bitstream identifier.

## Requirements

- Python 3.11 or newer
- FFmpeg and FFprobe
- ExifTool

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Load the environment variables from a private service manager or shell, then run:

```bash
set -a
. ./.env
set +a
.venv/bin/python app.py
```

The example host and identity values are placeholders. Replace them privately and never commit the real values.

## Validation

```bash
.venv/bin/python -m py_compile app.py tests/test_app.py
.venv/bin/python -m unittest discover -s tests -v
node --check static/app.js
git diff --check
```

## Privacy notes

The exact report is returned only in the processing response and rendered with text-only DOM APIs. At most 120 items and 240 characters per value are returned; omitted counts are reported. Metadata values are not placed in server logs or embedded in the output.

The public repository intentionally excludes deployment units, private hostnames, Tailnet URLs, owner identities, environment files, runtime state, uploaded files and QA evidence.

## License

No license is granted at this time. Public visibility does not grant permission to copy, modify or redistribute the code.
