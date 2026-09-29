from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import zoneinfo
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

# oauthlib refuses non-HTTPS OAuth by default. Our redirect URI is
# intentionally http://127.0.0.1 -- Google itself allows plain HTTP for
# loopback addresses, and this app is local-only (DESIGN.md §7), so this is
# not a real downgrade.
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

from google_auth_oauthlib.flow import Flow

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from video_uploader import desktop_windows, oauth_https_catcher, runtime_settings, scheduler, token_store
from video_uploader.config import load_config
from video_uploader.core.engine import CoreEngine
from video_uploader.core.types import Platform, PlatformJobStatus, PlatformMetadata
from video_uploader.db import create_db_and_tables, get_engine
from video_uploader.models import PlatformJob, UploadJob
from video_uploader.publishers.facebook import GRAPH_API_VERSION as FACEBOOK_GRAPH_VERSION
from video_uploader.publishers.tiktok import AUTH_URL as TIKTOK_AUTH_URL
from video_uploader.publishers.tiktok import SCOPES as TIKTOK_SCOPES
from video_uploader.publishers.youtube import SCOPES as YOUTUBE_SCOPES
from video_uploader.publishers.youtube import YouTubePublisher

FACEBOOK_SCOPES = (
    "pages_show_list,pages_manage_posts,pages_read_engagement,business_management,"
    "instagram_basic,instagram_content_publish"
)

config = load_config()
engine = get_engine(config.storage.db_path)
create_db_and_tables(engine)
config.storage.upload_dir.mkdir(parents=True, exist_ok=True)

# Structured logging (DESIGN.md §7): console + a rotating file alongside
# the SQLite DB and OAuth cert, so failures are diagnosable without
# re-running the app under a debugger. 5 x 1MB rotated files is plenty
# for a single-user local tool.
config.storage.db_path.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(
            config.storage.db_path.parent / "app.log", maxBytes=1_000_000, backupCount=5
        ),
    ],
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.run_startup_sweep()
    scheduler.run_startup_purge()
    scheduler.start()
    oauth_https_catcher.start()
    yield
    oauth_https_catcher.shutdown()
    scheduler.shutdown()


app = FastAPI(title="Video Uploader", lifespan=lifespan)

# TikTok's publisher is fully implemented, but live connection is paused --
# unaudited apps can only post to a Private account, and the user hasn't
# decided whether to set up a separate test account for it (see memory).
# Hidden from the UI rather than ripped out so it's a one-line change to
# bring back once that's resolved.
UI_HIDDEN_PLATFORMS = {"tiktok"}

# Only these platforms' APIs support attaching a custom thumbnail image --
# the upload form has a single thumbnail field applied to whichever
# selected platforms are in this set, silently skipped for the rest.
THUMBNAIL_SUPPORTED_PLATFORMS = {"youtube", "facebook"}

# Short-form/Reels-style placement guidance -- NOT hard limits (those are
# preflight.py's job, enforced server-side before upload). These are
# "optimal reach/full features" thresholds the platforms themselves
# recommend; a video over them still uploads fine, it just may not get
# Shorts/Reels placement. Checked client-side against the selected file
# (see index.html) so the warning shows before submitting, not after.
PLATFORM_SHORT_FORM_GUIDELINES = {
    "youtube": {"label": "YouTube Shorts", "max_duration_seconds": 180},
    "facebook": {"label": "Facebook Reels", "max_duration_seconds": 90},
    "instagram": {"label": "Instagram Reels (full features/reach)", "max_duration_seconds": 90},
    "tiktok": {"label": "TikTok", "max_duration_seconds": 600},
}

# In-memory only (reset on restart) -- single-user local app, no need for
# real persistence. _last_tag_search lets the Tag inspector tab show its
# last results when you navigate back to it without re-querying YouTube
# (search.list costs 100 quota units per call). _selected_upload_tags
# accumulates tags clicked there so they can prefill the Upload tab's Tags
# field, independent of whatever keyword is currently being viewed.
_last_tag_search: dict | None = None
_selected_upload_tags: list[str] = []

WEB_DIR = Path(__file__).parent
app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
templates = Jinja2Templates(directory=WEB_DIR / "templates")


def _format_datetime(value: datetime | None) -> str:
    if value is None:
        return ""
    # %-d/%-I (no leading zero) are glibc-only strftime extensions --
    # not portable to Windows, which this app runs on. %d/%I with a
    # leading zero stripped by hand keeps this working everywhere.
    aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    day = str(int(aware.strftime("%d")))
    hour = str(int(aware.strftime("%I")))
    return aware.strftime(f"%b {day}, %Y {hour}:%M %p UTC")


templates.env.filters["format_datetime"] = _format_datetime

TIMEZONE_NAMES = sorted(zoneinfo.available_timezones())


def _missed_platform_jobs(session: Session) -> list[PlatformJob]:
    """Jobs still genuinely MISSED and not dismissed from the Upload tab's
    banner. Dismissing (persisted via runtime_settings, so it survives a
    relaunch) never touches the job's actual status, so it stays MISSED
    in Job status/history exactly as it happened."""
    missed = session.exec(
        select(PlatformJob).where(PlatformJob.status == PlatformJobStatus.MISSED)
    )
    dismissed = runtime_settings.get_dismissed_missed_job_ids()
    return [pj for pj in missed if pj.id not in dismissed]


def _index_context(error: str | None = None) -> dict:
    with Session(engine) as session:
        missed_jobs = _missed_platform_jobs(session)
        # Access upload_job while the session is still open so the
        # template can read titles without a DetachedInstanceError.
        missed_jobs = [(pj, pj.upload_job) for pj in missed_jobs]
    visible_platforms = [p.value for p in Platform if p.value not in UI_HIDDEN_PLATFORMS]
    short_form_guidelines = {
        p: PLATFORM_SHORT_FORM_GUIDELINES[p]
        for p in visible_platforms
        if p in PLATFORM_SHORT_FORM_GUIDELINES
    }
    return {
        "platforms": visible_platforms,
        "timezones": TIMEZONE_NAMES,
        "missed_jobs": missed_jobs,
        "error": error,
        "selected_tags_csv": ", ".join(_selected_upload_tags),
        "short_form_guidelines": short_form_guidelines,
        "short_form_guidelines_json": json.dumps(short_form_guidelines),
    }


@app.get("/")
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", _index_context())


@app.post("/jobs")
def create_job(
    request: Request,
    video: UploadFile,
    title: str = Form(...),
    description: str = Form(""),
    tags: str = Form(""),
    privacy: str = Form("private"),
    platforms: list[str] = Form(default_factory=list),
    when: str = Form("now"),
    scheduled_at: str = Form(""),
    scheduled_tz: str = Form(""),
    thumbnail: UploadFile | None = File(default=None),
):
    if not platforms:
        return templates.TemplateResponse(
            request,
            "index.html",
            _index_context(error="Select at least one platform to publish to."),
            status_code=422,
        )

    publish_at: datetime | None = None
    tz: str | None = None
    if when == "schedule":
        if not scheduled_at or not scheduled_tz:
            return templates.TemplateResponse(
                request,
                "index.html",
                _index_context(error="Pick a date/time and timezone to schedule for later."),
                status_code=422,
            )
        local_dt = datetime.fromisoformat(scheduled_at).replace(tzinfo=ZoneInfo(scheduled_tz))
        publish_at = local_dt.astimezone(timezone.utc)
        tz = scheduled_tz

    dest = config.storage.upload_dir / video.filename
    with dest.open("wb") as out:
        shutil.copyfileobj(video.file, out)

    default_metadata = PlatformMetadata(
        title=title,
        description=description,
        tags=[t.strip() for t in tags.split(",") if t.strip()],
        privacy=privacy,
    )

    default_format_variants = {
        p: (config.platforms.get(p, {}).get("default_format") or None) for p in platforms
    }

    platform_overrides: dict[str, PlatformMetadata] = {}
    if thumbnail and thumbnail.filename:
        thumb_dest = config.storage.upload_dir / f"thumb_{thumbnail.filename}"
        with thumb_dest.open("wb") as out:
            shutil.copyfileobj(thumbnail.file, out)
        for platform in platforms:
            if platform not in THUMBNAIL_SUPPORTED_PLATFORMS:
                continue
            platform_overrides[platform] = PlatformMetadata(
                title=title,
                description=description,
                tags=default_metadata.tags,
                privacy=privacy,
                thumbnail_path=thumb_dest,
            )

    with Session(engine) as session:
        core_engine = CoreEngine(session)
        upload_job = core_engine.submit_job(
            video_path=dest,
            default_metadata=default_metadata,
            publish_at=publish_at,
            tz=tz,
            platforms=platforms,
            platform_overrides=platform_overrides,
            default_format_variants=default_format_variants,
        )
        if publish_at is None:
            core_engine.run_job(upload_job.id)
        # Scheduled jobs are picked up by the background poller (or, if
        # the app isn't running when they come due, swept into `missed`
        # on next launch) -- see scheduler.py.
        job_id = upload_job.id

    return RedirectResponse(url=f"/jobs/{job_id}?submitted=1", status_code=303)


@app.get("/jobs")
def list_jobs(request: Request):
    with Session(engine) as session:
        jobs = session.exec(select(UploadJob).order_by(UploadJob.created_at.desc())).all()
        return templates.TemplateResponse(
            request,
            "job_status.html",
            {"jobs": jobs, "retention_days": runtime_settings.get_retention_days()},
        )


@app.get("/jobs/{job_id}")
def job_detail(request: Request, job_id: int):
    with Session(engine) as session:
        job = session.get(UploadJob, job_id)
        if job is None:
            raise HTTPException(
                status_code=404,
                detail=f"Job #{job_id} not found -- it may have been purged from history "
                "(see the retention setting at the top of Job status).",
            )
        return templates.TemplateResponse(
            request,
            "job_status.html",
            {
                "jobs": [job],
                "retention_days": runtime_settings.get_retention_days(),
                "just_submitted": request.query_params.get("submitted") is not None,
            },
        )


@app.post("/jobs/retention-days")
def set_job_history_retention_days(retention_days: int = Form(...)):
    runtime_settings.set_retention_days(retention_days)
    return RedirectResponse(url="/jobs", status_code=303)


@app.post("/jobs/platform/{platform_job_id}/refresh")
def refresh_platform_job(platform_job_id: int):
    with Session(engine) as session:
        core_engine = CoreEngine(session)
        core_engine.refresh_platform_job_status(platform_job_id)
        platform_job = session.get(PlatformJob, platform_job_id)
        upload_job_id = platform_job.upload_job_id if platform_job else None
    return RedirectResponse(url=f"/jobs/{upload_job_id}", status_code=303)


@app.post("/jobs/platform/{platform_job_id}/retry")
def retry_platform_job(platform_job_id: int):
    """Used both as a general retry for FAILED rows and as 'Upload now'
    for MISSED rows -- re-running a job is the same operation either way."""
    with Session(engine) as session:
        core_engine = CoreEngine(session)
        core_engine.retry_platform_job(platform_job_id)
        platform_job = session.get(PlatformJob, platform_job_id)
        upload_job_id = platform_job.upload_job_id if platform_job else None
    return RedirectResponse(url=f"/jobs/{upload_job_id}", status_code=303)


@app.post("/jobs/platform/{platform_job_id}/dismiss-missed")
def dismiss_missed_platform_job(platform_job_id: int):
    """Hides this job from the Upload tab's missed-uploads banner only --
    doesn't touch its actual status, so it's untouched in Job status.
    Persisted, so it stays dismissed across a relaunch."""
    runtime_settings.dismiss_missed_job_ids([platform_job_id])
    return RedirectResponse(url="/", status_code=303)


@app.post("/jobs/missed/dismiss-all")
def dismiss_all_missed_platform_jobs():
    with Session(engine) as session:
        ids = [platform_job.id for platform_job in _missed_platform_jobs(session)]
    runtime_settings.dismiss_missed_job_ids(ids)
    return RedirectResponse(url="/", status_code=303)


@app.post("/jobs/{upload_job_id}/retry-failed")
def retry_failed_platform_jobs(upload_job_id: int):
    """Thin wrapper around retry_platform_job, called once per FAILED
    platform job in this upload job -- no new engine logic, matches the
    existing 'one platform at a time' retry semantics
    (DESIGN.md §6.11)."""
    with Session(engine) as session:
        core_engine = CoreEngine(session)
        upload_job = session.get(UploadJob, upload_job_id)
        if upload_job is None:
            raise HTTPException(status_code=404, detail="Upload job not found")
        failed_ids = [
            pj.id for pj in upload_job.platform_jobs if pj.status == PlatformJobStatus.FAILED
        ]
        for platform_job_id in failed_ids:
            core_engine.retry_platform_job(platform_job_id)
    return RedirectResponse(url=f"/jobs/{upload_job_id}", status_code=303)


@app.get("/tools/tags")
def tag_inspector(request: Request, keyword: str = ""):
    """DESIGN.md milestone 10: standalone lookup tool, not part of any
    PlatformJob. YouTube-only -- Facebook/Instagram/TikTok have no
    equivalent official API for this."""
    global _last_tag_search
    error: str | None = None

    if keyword.strip():
        try:
            publisher = YouTubePublisher()
            publisher.authenticate()
            tags = publisher.find_top_tags(keyword.strip())
            _last_tag_search = {"keyword": keyword.strip(), "tags": tags}
        except Exception as exc:  # noqa: BLE001 - surface as a page message, not a 500
            error = str(exc)
    elif _last_tag_search is not None:
        # No keyword in the URL -- e.g. clicked "Tag inspector" in the nav
        # rather than following a search link. Show the last search
        # instead of a blank form, without spending more YouTube quota.
        keyword = _last_tag_search["keyword"]

    tags = _last_tag_search["tags"] if _last_tag_search else []

    return templates.TemplateResponse(
        request,
        "tags.html",
        {
            "keyword": keyword,
            "tags": tags,
            "error": error,
            "selected_tags": _selected_upload_tags,
        },
    )


@app.post("/tools/tags/toggle")
def toggle_selected_tag(tag: str = Form(...), keyword: str = Form("")):
    """Clicking a tag in the results table adds/removes it from the set
    that prefills the Upload tab's Tags field -- accumulates across
    different keyword searches, not scoped to whichever one is on screen."""
    if tag in _selected_upload_tags:
        _selected_upload_tags.remove(tag)
    else:
        _selected_upload_tags.append(tag)
    return RedirectResponse(f"/tools/tags?keyword={quote(keyword)}", status_code=303)


@app.post("/tools/tags/clear-selected")
def clear_selected_tags(keyword: str = Form("")):
    _selected_upload_tags.clear()
    return RedirectResponse(f"/tools/tags?keyword={quote(keyword)}", status_code=303)


@app.get("/jobs/{upload_job_id}/reschedule")
def reschedule_form(request: Request, upload_job_id: int):
    with Session(engine) as session:
        upload_job = session.get(UploadJob, upload_job_id)
        if upload_job is None:
            raise HTTPException(status_code=404, detail="Upload job not found")

        current_tz = upload_job.scheduled_tz or "UTC"
        prefill = None
        if upload_job.scheduled_for is not None:
            # scheduled_for round-trips through SQLite as a naive datetime
            # that is implicitly UTC (that's all we ever store); attach
            # tzinfo explicitly before converting to the display zone.
            aware_utc = upload_job.scheduled_for.replace(tzinfo=timezone.utc)
            prefill = aware_utc.astimezone(ZoneInfo(current_tz)).strftime("%Y-%m-%dT%H:%M")

        return templates.TemplateResponse(
            request,
            "reschedule.html",
            {
                "upload_job": upload_job,
                "timezones": TIMEZONE_NAMES,
                "current_tz": current_tz,
                "prefill": prefill,
            },
        )


@app.post("/jobs/{upload_job_id}/reschedule")
def reschedule_submit(
    upload_job_id: int,
    scheduled_at: str = Form(...),
    scheduled_tz: str = Form(...),
):
    local_dt = datetime.fromisoformat(scheduled_at).replace(tzinfo=ZoneInfo(scheduled_tz))
    publish_at = local_dt.astimezone(timezone.utc)

    with Session(engine) as session:
        core_engine = CoreEngine(session)
        core_engine.reschedule_upload_job(upload_job_id, publish_at=publish_at, tz=scheduled_tz)

    return RedirectResponse(url=f"/jobs/{upload_job_id}", status_code=303)


@app.get("/settings")
def settings(request: Request):
    connected = {p.value: token_store.load_token(p.value) is not None for p in Platform}

    youtube_channel_name = None
    youtube_channel_error = None
    if connected["youtube"]:
        try:
            publisher = YouTubePublisher()
            publisher.authenticate()
            youtube_channel_name = publisher.get_connected_channel_name()
        except Exception as exc:  # noqa: BLE001 - surface as a page message, not a 500
            youtube_channel_error = str(exc)

    facebook_page_name = None
    if connected["facebook"]:
        # Captured once at connect time (see oauth_https_catcher.py) --
        # unlike YouTube, no live API round trip needed to show this.
        facebook_page_name = token_store.load_token("facebook").get("page_name")

    # TikTok has no separate "connected account name" display -- DESIGN.md
    # §3/§8 already means every TikTok upload posts as private/self-view
    # regardless of account, so there's no channel/page-style identity
    # worth surfacing here the way YouTube's/Facebook's rows do.

    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "platforms": [p.value for p in Platform if p.value not in UI_HIDDEN_PLATFORMS],
            "connected": connected,
            "youtube_channel_name": youtube_channel_name,
            "youtube_channel_error": youtube_channel_error,
            "facebook_page_name": facebook_page_name,
            "config_path": str(config.config_path),
            "config_file_changed": request.query_params.get("config_file_changed") is not None,
        },
    )


def _reload_config_from(path: Path) -> None:
    """Swaps in a newly-chosen config.yaml's platform credentials live, no
    restart needed. Storage location (SQLite DB/uploads) and server
    host/port stay anchored to this install and this running server, so
    they're deliberately left untouched here."""
    new_config = load_config(path)
    config.platforms = new_config.platforms
    config.config_path = new_config.config_path


@app.post("/settings/theme")
def set_theme(theme: str = Form("dark")):
    if theme not in ("dark", "light"):
        theme = "dark"
    response = RedirectResponse("/settings", status_code=303)
    # A year is effectively "forever" for a locally-run app with no
    # server-side account -- there's nothing else to key this to.
    response.set_cookie("theme", theme, max_age=60 * 60 * 24 * 365)
    return response


@app.post("/settings/choose-config-file")
def choose_config_file():
    """Opens the same native file picker launcher.py uses on startup, lets
    the user reuse an existing config.yaml or create a new one anywhere,
    and applies it immediately -- no app restart required."""
    from video_uploader.launcher import choose_new_config_file

    chosen = choose_new_config_file()
    if chosen is not None:
        _reload_config_from(chosen)
        return RedirectResponse("/settings?config_file_changed=1", status_code=303)
    return RedirectResponse("/settings", status_code=303)


def _youtube_client_config() -> dict:
    platform_config = config.platforms.get("youtube", {})
    return {
        "web": {
            "client_id": platform_config.get("client_id", ""),
            "client_secret": platform_config.get("client_secret", ""),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }


# Keyed by OAuth `state`. Holds the in-flight Flow object between
# /start and /callback, since google-auth-oauthlib generates a PKCE
# code_verifier on the Flow instance itself -- a freshly-constructed Flow
# in the callback wouldn't have it, and Google's token endpoint rejects
# the exchange without it ("Missing code verifier"). Fine to keep
# in-process/in-memory: this is a single-user, single-process local app.
_pending_oauth_flows: dict[str, Flow] = {}


@app.get("/oauth/youtube/start")
def youtube_oauth_start(request: Request):
    flow = Flow.from_client_config(_youtube_client_config(), scopes=YOUTUBE_SCOPES)
    flow.redirect_uri = str(request.url_for("youtube_oauth_callback"))
    auth_url, state = flow.authorization_url(access_type="offline", prompt="consent")
    _pending_oauth_flows[state] = flow
    return RedirectResponse(auth_url)


@app.get("/oauth/youtube/callback", name="youtube_oauth_callback")
def youtube_oauth_callback(request: Request):
    state = request.query_params.get("state")
    flow = _pending_oauth_flows.pop(state, None)
    if flow is None:
        raise HTTPException(
            status_code=400,
            detail="Unknown or expired OAuth state -- please try connecting again from /settings.",
        )
    flow.fetch_token(authorization_response=str(request.url))

    credentials = flow.credentials
    token_store.save_token(
        "youtube",
        {
            "token": credentials.token,
            "refresh_token": credentials.refresh_token,
            "scopes": list(credentials.scopes or YOUTUBE_SCOPES),
        },
    )
    settings_url = f"http://{config.server.host}:{config.server.port}/settings"
    desktop_windows.close_popup()
    desktop_windows.refresh_main_window(settings_url)
    return RedirectResponse(url="/settings")


def _facebook_client_config() -> dict:
    platform_config = config.platforms.get("facebook", {})
    return {
        "app_id": platform_config.get("app_id", ""),
        "app_secret": platform_config.get("app_secret", ""),
        "page_id": platform_config.get("page_id", ""),
    }


@app.get("/oauth/facebook/start")
def facebook_oauth_start():
    # The callback itself is handled by the separate HTTPS catcher
    # (video_uploader.oauth_https_catcher) -- Meta requires HTTPS for the
    # redirect URI with no way to disable that for this app, unlike
    # Google's loopback exemption that lets YouTube's flow stay on plain
    # HTTP. This route only builds the outbound dialog URL; that request
    # itself has no HTTPS requirement.
    fb_config = _facebook_client_config()
    state = secrets.token_urlsafe(24)
    oauth_https_catcher.pending_states.add(state)
    auth_url = (
        f"https://www.facebook.com/{FACEBOOK_GRAPH_VERSION}/dialog/oauth"
        f"?client_id={fb_config['app_id']}&redirect_uri={oauth_https_catcher.CALLBACK_URL}"
        f"&state={state}&scope={FACEBOOK_SCOPES}"
    )
    return RedirectResponse(auth_url)


def _tiktok_client_config() -> dict:
    platform_config = config.platforms.get("tiktok", {})
    return {
        "client_key": platform_config.get("client_key", ""),
        "client_secret": platform_config.get("client_secret", ""),
    }


@app.get("/oauth/tiktok/start")
def tiktok_oauth_start():
    # Same HTTPS-catcher setup as Facebook -- TikTok also requires HTTPS
    # redirect URIs with no loopback exemption. Unlike Instagram, TikTok
    # has a fully independent OAuth identity (not derived from another
    # platform's connection), so this is its own dedicated start route.
    tiktok_config = _tiktok_client_config()
    state = secrets.token_urlsafe(24)
    oauth_https_catcher.pending_states.add(state)
    auth_url = (
        f"{TIKTOK_AUTH_URL}?client_key={tiktok_config['client_key']}"
        f"&response_type=code&scope={TIKTOK_SCOPES}"
        f"&redirect_uri={oauth_https_catcher.TIKTOK_CALLBACK_URL}&state={state}"
    )
    return RedirectResponse(auth_url)


def _wait_until_serving(host: str, port: int, timeout_seconds: float = 10.0) -> None:
    """Blocks until something is accepting connections on host:port, so the
    desktop window isn't opened against a server that hasn't bound yet."""
    import socket
    import time

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"Server did not start listening on {host}:{port} in time")


class DesktopApi:
    """Exposed to the main window's page JS as `pywebview.api` (see
    js_api= below) -- lets Settings ask Python to open a second native
    window for a platform's OAuth connect flow, instead of navigating the
    main window away to an external site."""

    def open_oauth_popup(self, platform: str) -> None:
        if platform not in ("youtube", "facebook", "tiktok"):
            return
        url = f"http://{config.server.host}:{config.server.port}/oauth/{platform}/start"
        desktop_windows.open_popup(f"Connect {platform.capitalize()}", url)


def main() -> None:
    """Entry point for both the `video-uploader` console script
    (pyproject.toml's [project.scripts]) and run.py. Runs the FastAPI app on
    a background thread and shows it in a native desktop window (no browser
    chrome) via pywebview."""
    import threading

    import uvicorn
    import webview

    def run_server() -> None:
        uvicorn.run(app, host=config.server.host, port=config.server.port, log_level="warning")

    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()
    _wait_until_serving(config.server.host, config.server.port)

    main_window = webview.create_window(
        "Video Uploader",
        f"http://{config.server.host}:{config.server.port}",
        width=1100,
        height=850,
        min_size=(700, 500),
        # pywebview defaults this to False (feels more "native app"-like),
        # but this app's whole point is showing IDs/URLs/error messages the
        # user needs to copy elsewhere.
        text_select=True,
        js_api=DesktopApi(),
    )
    desktop_windows.set_main_window(main_window)
    webview.start()
