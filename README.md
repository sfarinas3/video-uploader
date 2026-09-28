# Video Uploader

A locally-run tool that publishes a video to multiple social platforms
using credentials you provide. No third-party server is involved — it
runs entirely on your machine, with a small local web UI at
`http://127.0.0.1:8000`.

See [docs/DESIGN.md](docs/DESIGN.md) for the full design and
requirements doc, including the milestone roadmap and each platform's
specific constraints.

## Platform status

| Platform | Status |
|---|---|
| YouTube | Implemented |
| Facebook (Page) | Implemented |
| Instagram (Business, via a linked Facebook Page) | Implemented |
| TikTok | Implemented, but live connection is currently paused — unaudited apps can only post to TikTok accounts that are themselves set to Private, and that tradeoff hasn't been made yet for the main account. See `docs/DESIGN.md` for the plan (a disposable test account) once that's decided. |

## Setup

1. Copy `config.example.yaml` to `config.yaml` (gitignored — never commit
   this file). Each platform's section has comments describing exactly
   what developer-app setup is needed (redirect URIs, permissions/scopes,
   etc.) and where to get `client_id`/`client_secret`-style credentials.
2. Install dependencies (a virtualenv is recommended):
   ```
   pip install -e .
   ```
3. `ffmpeg`/`ffprobe` must be installed and on `PATH` — used for
   pre-flight video validation (duration, codec, aspect ratio) and by the
   live smoke test scripts in `scripts/`.

## Running

```
python run.py
```

or, after `pip install -e .`, the equivalent console script:

```
video-uploader
```

This opens the app in its own desktop window (via `pywebview`), not a
browser tab. On first use, visit Settings to connect each platform you
want to publish to — this walks you through that platform's OAuth flow.

Logs are written to both the console and `data/app.log` (rotated, kept
locally, gitignored along with the SQLite job database).

## Desktop app (.exe)

To build a standalone Windows executable with a custom icon, for a
proper double-click-to-launch/pin-to-taskbar desktop app:

```
pip install -e ".[build]"
.\scripts\build_exe.ps1
```

This produces `dist\video-uploader.exe`. Frozen builds read
`config.yaml`/`data/` from next to the exe rather than the source tree,
so copy `config.example.yaml` alongside it as `config.yaml` and fill in
credentials (the build script copies `config.example.yaml` there for
you). From there you can create a shortcut to the exe and pin it to the
desktop/taskbar like any other app.

## Running tests

```
pytest
```

The regular suite is fully offline (no network or real credentials
touched — fakes stand in for every platform's API). Each platform also
has a live smoke test script under `scripts/` (e.g.
`scripts/youtube_smoke_test.py`) that exercises the real API end-to-end
against your own connected account; see `docs/DESIGN.md` §10 for the
testing policy these follow (most-restricted visibility, always cleaned
up afterward).
