# BhashaFrame V2 — Multilingual Video Subtitle Studio

This fresh project uses the local Whisper + FFmpeg pipeline from the original [Video_transcriber repository](https://github.com/Jeetthakkar16/Video_transcriber), wrapped in a web UI and timestamp-aware editor.

## Included
- Dark charcoal UI, lime accent, Inter typography, mono micro-labels, source/editor/export card layout.
- Whisper model picker: tiny, base, small, medium, large, turbo.
- Auto language detection plus Indian and international language choices.
- CPU or NVIDIA CUDA selection.
- Upload-based jobs, processing status, persisted job metadata, SRT/VTT/TXT exports.
- Editable text/start/end times and subtitle preview synchronized with video playback.
- FFprobe stream checks, normalized FFmpeg audio extraction, audio/video start-offset compensation, segment clamping to duration.
- MP4 subtitle burn-in with FFmpeg/libass and original audio stream copied.

## Windows quick start
1. Install Python 3.11.
2. Install FFmpeg, including ffprobe, and add both to PATH. Open a new Command Prompt and verify \`ffmpeg -version\` and \`ffprobe -version\`.
3. Extract this project.
4. Run \`install.bat\`, then \`run.bat\`.
5. Open http://127.0.0.1:8000.

Manual setup:
\`\`\`powershell
py -3.11 -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
\`\`\`

The model downloads on first use. Transcription then runs locally. CPU is compatible but slower; CUDA requires a compatible GPU build of PyTorch.

## Use
1. Upload a video, select spoken language (or Auto detect), model, compute device, and processing mode.
2. Generate subtitles and wait for the completed state.
3. Edit a segment's text or start/end timestamps. Use \`mm:ss.cc\`, \`hh:mm:ss.cc\`, or raw seconds. Click Save changes.
4. Download SRT/VTT/TXT, or Render MP4.

## API
- GET \`/api/health\`
- GET \`/api/settings\`
- POST \`/api/jobs\`
- GET \`/api/jobs\` and GET \`/api/jobs/{id}\`
- PUT \`/api/jobs/{id}/subtitles\`
- POST \`/api/jobs/{id}/settings\`
- POST \`/api/jobs/{id}/export-video\`
- GET \`/api/jobs/{id}/download/{srt|vtt|txt|video}\`
- DELETE \`/api/jobs/{id}\`
- Swagger UI: http://127.0.0.1:8000/docs

## Synchronization design
The app probes video/audio stream start times, extracts mono 16 kHz WAV, and compensates when the audio stream starts later than the video stream. Timestamps are clamped to known media duration and invalid intervals are normalized. Subtitle edits are persisted to all sidecar files, and MP4 rendering uses the latest saved segment list.

No generic timestamp algorithm can guarantee perfect sync for all damaged, variable-frame-rate, or unusual media. Inspect generated captions on the source before relying on the render for production.

## Translation behavior
Whisper can transcribe speech in its selected/detected language and has a built-in translate-to-English task. It does not translate into arbitrary target languages. This version therefore offers transcription and Whisper's English-translation mode without pretending that a local subtitle target-language translator is included.

## Tests
Install pytest, then run \`python -m pip install pytest\` and \`python -m pytest -q\`. The test import may require all dependencies in requirements.txt to be installed.

## Production note
This is a local single-process foundation, not a multi-tenant SaaS deployment. Job state is partly in memory and local disk; before public use, add authentication, per-user quotas, isolated worker queue, database-backed job state, storage cleanup, and audit logging.
