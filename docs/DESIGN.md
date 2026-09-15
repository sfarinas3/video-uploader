# Multi-Platform Video Uploader — Design & Requirements

Status: Draft
Owner: s.farinas3@gmail.com
Last updated: 2026-09-14

## 1. Purpose

A locally-run Python application that takes a single video plus metadata and
publishes it to multiple social media platforms (YouTube, Facebook, Instagram,
TikTok) using credentials the user supplies. No third-party server is
involved — the app runs entirely on the user's machine.

## 2. Goals / Non-Goals

**Goals**
- One video in, N platform uploads out, tracked individually.
- Per-platform metadata (title, description, tags, privacy, thumbnail).
- Credentials and tokens stored locally and securely (OS keychain).
- Easy to add/remove a platform later without touching core logic.
- Clear, per-platform error reporting — one platform failing must not block
  the others.

**Non-Goals (v1)**
- No hosted/multi-user service. Single local user only, own accounts only —
  no support for managing other people's accounts.
- No video editing/transcoding beyond basic pre-flight validation.
- No public TikTok posting via the API. Unaudited apps can only post as
  private/self-view; flipping a TikTok video to public is a manual step the
  user performs in the TikTok app itself (see §3 and §8).

## 3. Platform Feasibility Summary

| Platform | API | Account requirement | Known constraints |
|---|---|---|---|
| YouTube | YouTube Data API v3 (`videos.insert`) | Any Google account | Default quota 10,000 units/day; an upload costs 1,600 units (~6/day) unless quota increase is granted |
| Facebook | Graph API (`/{page-id}/videos`) | Facebook **Page** (not personal profile) | Publishing permissions need App Review, or the dev app can stay in "development mode" with the user's own account added as a tester |
| Instagram | Instagram Graph API (Content Publishing) | Instagram **Business/Creator** account linked to a Facebook Page | Two-step publish: create media container, then publish it |
| TikTok | Content Posting API | TikTok Developer app | Unaudited apps can only post as private/self-view; public posting requires TikTok's app audit process |

Each platform requires the **user** to register their own developer
application (client ID/secret) on that platform's console — this tool
consumes those credentials, it does not obtain them on the user's behalf.

**TikTok private-to-public flip**: the API itself has no "make public" call
available to an unaudited app — the privacy level is fixed at upload time to
what the app's audit tier allows. The app will upload as private/self-view
and record the resulting TikTok video in the job history; the user then opens
the TikTok app and changes the privacy setting there by hand. This is a
manual, out-of-band step this tool cannot automate until (if) the developer
app passes audit.

## 4. Architecture Overview

```
                         ┌───────────────────────┐
                         │   Local Web UI (FastAPI)│
                         │  - upload form           │
                         │  - job status view       │
                         └──────────┬───────────────┘
                                    │
                         ┌──────────▼───────────────┐
                         │      Scheduler            │
                         │  - due-job polling        │
                         │  - "publish now" vs later │
                         └──────────┬───────────────┘
                                    │
                         ┌──────────▼───────────────┐
                         │      Core Engine          │
                         │  - job orchestration      │
                         │  - metadata mapping       │
                         │  - preflight validation   │
                         └──────────┬───────────────┘
                                    │
          ┌───────────┬────────────┼────────────┬───────────┐
          ▼           ▼            ▼            ▼
     ┌─────────┐ ┌──────────┐ ┌───────────┐ ┌──────────┐
     │ YouTube │ │ Facebook │ │ Instagram │ │  TikTok  │
     │Publisher│ │Publisher │ │ Publisher │ │Publisher │
     └─────────┘ └──────────┘ └───────────┘ └──────────┘
          │           │            │            │
          └───────────┴─── OS Keychain (tokens) ┘
```

### 4.1 Core Engine
- Accepts a video file + a set of per-platform metadata.
- Runs pre-flight validation (file size, duration, codec, aspect ratio) per
  target platform before attempting upload.
- Fans the video out into one job per selected platform.
- Tracks each job's state: `pending → uploading → processing → published |
  failed`.
- Retries transient failures (network errors, rate limits) with backoff;
  surfaces permanent failures (auth expired, validation rejected) directly.

### 4.2 Publisher Interface

Each platform implements a common interface so new platforms can be added
without changing the core engine:

```python
class Publisher(Protocol):
    def authenticate(self) -> None: ...
    def validate(self, video: VideoFile, metadata: PlatformMetadata) -> list[str]: ...  # returns validation errors
    def upload(self, video: VideoFile, metadata: PlatformMetadata) -> JobHandle: ...
    def get_status(self, job: JobHandle) -> JobStatus: ...
```

### 4.3 Credential Storage
- **App-level credentials** (client ID/secret per platform, registered by the
  user on each platform's developer console): stored in a local config file
  (`config.yaml` or `.env`), excluded from version control.
- **User OAuth tokens** (access/refresh tokens obtained after the user
  authorizes the app): stored in the OS keychain via the `keyring` library —
  never written to plain files.
- OAuth flow uses a local redirect URI (`http://localhost:<port>/callback`)
  to capture the authorization code.

### 4.4 Scheduler
- Every upload is either "publish now" (immediate) or "publish at" a
  user-chosen future date/time.
- The scheduling form includes a timezone selector, defaulting to the local
  machine's timezone; the user can override it per scheduled job (e.g. to
  schedule in a specific audience's timezone). The resolved UTC instant is
  what's actually stored and scheduled against, with the chosen timezone kept
  alongside it for display purposes.
- Scheduled uploads are persisted in SQLite (video path, metadata, target
  platforms, `scheduled_for` UTC timestamp, display timezone) as soon as
  they're created — not held in memory — so a scheduled job survives an app
  restart as long as the app is running again before its due time.
- A background poller (APScheduler, or a simple periodic task) checks for due
  jobs and hands them to the Core Engine when their time arrives.
- The app must be running at the scheduled time for the job to fire; this is
  a local scheduler, not a cloud one. If the app was closed through the
  scheduled time, the job is marked `missed`.
- **Missed-job recovery**: on every app launch, the app checks for `missed`
  jobs and, if any exist, surfaces a warning to the user (in the web UI, and
  optionally on the CLI/console output at startup) listing each one. For each
  missed job the user is given two options: **upload now** (publish
  immediately, as-is) or **reschedule** (pick a new date/time, going back
  through the same timezone-aware scheduling form). A missed job is never
  auto-published without the user choosing one of these explicitly.

### 4.5 Local Web UI
- Single-page form: pick a video file, fill in a default title/description,
  optionally override per-platform, select target platforms, optionally
  attach a thumbnail per platform, choose "publish now" or a scheduled
  date/time, submit.
- Job status view: per-platform progress and errors for the current and past
  uploads, plus pending/missed scheduled jobs (in-memory or a local SQLite
  table — no external DB).

## 5. Tech Stack

- **Language**: Python 3.12+
- **Web framework**: FastAPI + Uvicorn (local-only, binds to `127.0.0.1`)
- **UI**: server-rendered HTML (Jinja2) + minimal JS — no SPA framework needed
  for v1
- **Credential storage**: `keyring` (OS keychain integration)
- **HTTP/OAuth**: `httpx` + `authlib` (or platform SDKs where available,
  e.g. `google-api-python-client` for YouTube)
- **Video inspection**: `ffmpeg`/`ffprobe` (via subprocess) for pre-flight
  validation (duration, resolution, codec)
- **Local persistence**: SQLite (job history/status/scheduled jobs) via
  `sqlite3` or `sqlmodel`
- **Scheduling**: `APScheduler` with its SQLAlchemy job store (backed by the
  same local SQLite file) so scheduled jobs survive an app restart
- **Packaging**: run via `uv`/`pip` + a simple `run.py`; a bundled
  executable (PyInstaller) can be a later nice-to-have

## 6. Functional Requirements

1. User can select a local video file and enter title/description/tags.
2. User can select which platforms to publish to for a given upload.
3. User can override metadata per platform (e.g., different title for
   YouTube vs. Instagram caption).
4. User can optionally attach a custom thumbnail image per platform; if none
   is provided, the platform's own default (e.g., an auto-picked frame) is
   used.
5. User can choose to publish immediately or schedule the upload for a
   future date/time in a timezone of their choosing (defaulting to the local
   machine's timezone); scheduled jobs persist across app restarts and fire
   automatically once the app is running at the scheduled time.
6. App validates the video against each selected platform's known
   constraints before uploading, and reports validation failures without
   attempting the upload.
7. App uploads to each selected platform independently; failure on one
   platform does not stop or roll back others.
8. TikTok uploads are always created as private/self-view; the app clearly
   labels this in the UI and does not claim to make the post public.
9. App persists OAuth tokens so the user does not need to re-authenticate on
   every run; tokens refresh automatically when expired.
10. User can view the status and any error detail for each platform job,
    including pending and missed scheduled jobs.
11. User can retry a failed or missed platform job without re-uploading to
    platforms that already succeeded.
12. On launch, if any scheduled job is `missed`, the app warns the user and
    requires them to choose, per missed job, either "upload now" or
    "reschedule" — a missed job is never auto-published silently.

## 7. Non-Functional Requirements

- **Local-only**: the web UI binds to localhost only; no data leaves the
  machine except direct calls to each platform's official API.
- **Security**: no plaintext storage of tokens or app secrets; config file
  with secrets must be gitignored by default.
- **Resilience**: network hiccups during upload should retry with backoff,
  not fail the whole job outright.
- **Extensibility**: adding a new platform should only require a new
  `Publisher` implementation, not core engine changes.
- **Observability**: structured logs per job/platform to make failures
  diagnosable (e.g., which API call failed and why).
- **Safe testing**: any test upload against a live platform API must default
  to that platform's most private visibility option and must be deleted
  after the test concludes (see §10).

## 8. Decisions

- **TikTok**: ship v1 with TikTok uploads as private/self-view. The flip to
  public is a manual step the user does in the TikTok app itself — the API
  does not expose a way to do this for an unaudited developer app, so this
  tool cannot automate it (see §3).
- **Facebook/Instagram**: single-user, own-accounts-only. No multi-user or
  managing-other-accounts support is in scope.
- **Thumbnails**: optional, per-platform. If omitted, the platform's own
  default behavior applies.
- **Scheduling**: in scope for v1 as a local scheduler (see §4.4). Not a
  cloud/always-on scheduler — the app must be running at the scheduled time.
- **Testing against live APIs**: test uploads always use the platform's most
  private visibility setting and are deleted once the test is done (see
  §10). No test upload is ever left public or left live indefinitely.
- **Missed scheduled jobs**: never auto-publish late. On next launch, the app
  warns about any `missed` job and requires the user to explicitly choose
  "upload now" or "reschedule" for each one (see §4.4).
- **Timezone handling**: the scheduler is timezone-aware and configurable per
  job, defaulting to the local machine's timezone (see §4.4).

## 9. Open Questions / Risks

None outstanding at this time.

## 10. Testing Strategy

Unit tests cover the core engine, job model, scheduler, and validation logic
against mocked platform APIs — no live API calls involved.

For each publisher, though, mocks can't catch real API drift (auth scope
changes, payload format changes, quota behavior), so each one also needs an
occasional **live smoke test** against the real platform using the user's own
test/dev account. That live test procedure is fixed as part of this project's
testing policy:

1. Use a short, clearly-labeled dummy video (e.g. `test-upload-do-not-use.mp4`)
   with an unambiguous test title/description (e.g. prefixed
   `[TEST - safe to delete]`) so it's never mistaken for real content.
2. Upload it via the target publisher with visibility forced to that
   platform's most private option, regardless of what the test parameters
   ask for:
   - **YouTube**: `privacyStatus = private`.
   - **Facebook**: page video published with the most restricted audience
     setting available via the API (or as an unpublished/draft video if the
     API supports it).
   - **Instagram**: there is no private-post option for Business/Creator
     content, so IG live smoke tests should instead target the same
     dev/tester account and be deleted immediately after confirming the
     upload succeeded — minimizing exposure time rather than avoiding
     visibility entirely (see caveat below).
   - **TikTok**: already private/self-view by construction for an unaudited
     app (§3, §8), so no extra override is needed here.
3. Verify the upload succeeded (status checks, ID returned, etc.).
4. Delete the test upload immediately after verification, via the platform's
   API where a delete endpoint exists (confirmed available for YouTube and
   Facebook). Record the returned post/video ID in the test log so cleanup
   can be confirmed or retried if the delete call itself fails.
5. Where the platform's API doesn't expose a delete endpoint for published
   content (Instagram Content Publishing API has no delete-published-media
   call; TikTok's Content Posting API likewise has no delete endpoint), the
   test harness itself prompts the user, blocking further progress: it
   prints/displays the post's direct URL or ID and a clear instruction to
   open it and delete it manually right now, then waits for the user to
   confirm deletion is done before marking that test run complete. The test
   is not considered finished — pass or fail — until this confirmation is
   given.
6. This clean-up step is mandatory for every live smoke test run, including
   ones run during local development — never leave a test video live on any
   platform, public or private, once the test's purpose is served.

## 11. Milestones (proposed)

1. Core engine + job model + SQLite persistence + local web UI skeleton.
2. YouTube publisher (simplest, best-documented API) end-to-end, including
   its live smoke test + cleanup per §10.
3. Scheduler (APScheduler + SQLite job store) wired into the core engine.
4. Facebook Page publisher + live smoke test/cleanup.
5. Instagram publisher (depends on Facebook Graph API groundwork) + live
   smoke test/cleanup.
6. TikTok publisher (private/self-view mode) + live smoke test/cleanup.
7. Optional per-platform thumbnail upload support.
8. Pre-flight validation via ffprobe for all platforms.
9. Polish: retry UX, job history view, packaging.
