"""
leonCAST - API layer
FastAPI app exposing station management, uploads, playlists, schedule,
and live status. This is what the dashboard UI talks to.
"""

import shutil
import logging
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Depends, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from data import db
from core.station_engine import StationConfig, Track
from core.station_manager import manager
from core.icecast_config import write_icecast_config
from core.scheduler import scheduler
from core.jingles import jingle_service
from api import auth

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("leoncast.api")

MEDIA_ROOT = Path(__file__).parent.parent / "media"
MEDIA_ROOT.mkdir(exist_ok=True)
static_dir = Path(__file__).parent.parent / "static"


def _regenerate_icecast_config():
    """Regenerate icecast.xml using the stored global settings instead of
    the previous hardcoded 'localhost'/'changeme' defaults. Call this any
    time a station or the global settings change."""
    settings = db.get_settings()
    write_icecast_config(
        hostname=settings["public_hostname"] or settings["icecast_internal_host"],
        port=int(settings["icecast_port"]),
        admin_password=settings["icecast_admin_password"],
        default_source_password=settings["icecast_default_source_password"],
    )


def _sync_auto_queue(station_id: str):
    """Self-queuing: push everything currently in the station's uploaded
    track library into its live playback queue, in upload order. Called
    after every upload/delete (and on startup rehydration) so a station
    always has something to play as soon as tracks exist -- no manual
    playlist creation/activation required for the common case. Manually
    activating a curated playlist (see /playlists/{id}/activate) still
    works and simply overrides this until the library changes again."""
    rows = db.list_tracks(station_id)
    tracks = [
        Track(id=r["id"], path=r["path"], title=r["title"], artist=r["artist"],
              duration=r["duration"], bpm=r["bpm"])
        for r in rows
    ]
    try:
        manager.set_playlist(station_id, tracks)
    except KeyError:
        pass  # engine not created yet; nothing to sync

app = FastAPI(title="leonCAST")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup():
    db.init_db()
    # Rehydrate engines for any stations already in the DB
    for row in db.list_stations():
        cfg = StationConfig(
            station_id=row["station_id"],
            name=row["name"],
            icecast_host=row["icecast_host"],
            icecast_port=row["icecast_port"],
            icecast_mount=row["icecast_mount"],
            icecast_source_password=row["icecast_source_password"],
            bitrate_kbps=row["bitrate_kbps"],
            crossfade_seconds=row["crossfade_seconds"],
            sample_rate=row["sample_rate"],
        )
        try:
            manager.create_station(cfg)
            _sync_auto_queue(row["station_id"])
        except ValueError:
            pass
    scheduler.start()
    jingle_service.start()
    _regenerate_icecast_config()
    logger.info("leonCAST API ready")


# ---------- schemas ----------

class StationCreateRequest(BaseModel):
    station_id: str
    name: str
    icecast_host: Optional[str] = None
    icecast_port: Optional[int] = None
    icecast_mount: str
    icecast_source_password: str
    bitrate_kbps: int = 128
    crossfade_seconds: float = 3.0
    sample_rate: int = 44100
    jingle_interval_tracks: int = 0  # 0 = disabled


class JingleIntervalRequest(BaseModel):
    interval_tracks: int


class StationSettingsRequest(BaseModel):
    background_url: str = ""
    background_color: str = "#05070a"
    public_stream_url: str = ""


class GlobalSettingsRequest(BaseModel):
    public_hostname: str = ""
    icecast_internal_host: str = "localhost"
    icecast_port: str = "8000"
    icecast_admin_password: str = "changeme"
    icecast_default_source_password: str = "changeme"


class PlaylistCreateRequest(BaseModel):
    station_id: str
    name: str
    track_ids: list[int] = []


class ScheduleBlockRequest(BaseModel):
    station_id: str
    playlist_id: int
    day_of_week: int  # 0=Mon..6=Sun, -1=every day
    start_time: str
    end_time: str


class SetupAdminRequest(BaseModel):
    username: str
    password: str


class LoginRequest(BaseModel):
    username: str
    password: str


class CreateUserRequest(BaseModel):
    username: str
    password: str
    is_admin: bool = False


# ---------- auth endpoints ----------

@app.get("/api/auth/status")
def api_auth_status():
    return {"needs_setup": not db.any_users_exist()}


@app.post("/api/auth/setup")
def api_auth_setup(req: SetupAdminRequest):
    """Create the first admin account. Only works while zero users exist --
    this is the one and only unauthenticated account-creation path."""
    if db.any_users_exist():
        raise HTTPException(400, "Setup already completed; log in instead")
    if len(req.username) < 3 or len(req.password) < 8:
        raise HTTPException(400, "Username must be 3+ chars, password 8+ chars")

    password_hash, salt = auth.hash_password(req.password)
    user_id = db.insert_user(req.username, password_hash, salt, is_admin=True)
    token = auth.issue_session(user_id)
    return {"token": token, "username": req.username, "is_admin": True}


@app.post("/api/auth/login")
def api_auth_login(req: LoginRequest):
    user = db.get_user_by_username(req.username)
    if not user or not auth.verify_password(req.password, user["password_hash"], user["salt"]):
        raise HTTPException(401, "Invalid username or password")
    token = auth.issue_session(user["id"])
    return {"token": token, "username": user["username"], "is_admin": bool(user["is_admin"])}


@app.post("/api/auth/logout")
def api_auth_logout(authorization: str = Header(default=None)):
    if authorization and authorization.startswith("Bearer "):
        db.delete_session(authorization.removeprefix("Bearer ").strip())
    return {"status": "logged out"}


@app.get("/api/auth/me")
def api_auth_me(user: dict = Depends(auth.get_current_user)):
    return {"username": user["username"], "is_admin": bool(user["is_admin"])}


# ---------- global settings (networking / icecast) ----------

@app.get("/api/settings")
def api_get_settings(user: dict = Depends(auth.get_current_user)):
    auth.require_admin(user)
    return db.get_settings()


@app.put("/api/settings")
def api_update_settings(req: GlobalSettingsRequest, user: dict = Depends(auth.get_current_user)):
    auth.require_admin(user)
    try:
        port = int(req.icecast_port)
        if not (1 <= port <= 65535):
            raise ValueError
    except ValueError:
        raise HTTPException(400, "Icecast port must be a number between 1 and 65535")

    db.update_settings(req.model_dump())
    _regenerate_icecast_config()
    return {"status": "updated", **db.get_settings()}


@app.post("/api/settings/regenerate-icecast-config")
def api_regenerate_icecast_config(user: dict = Depends(auth.get_current_user)):
    """Manually re-write icecast.xml from current settings/stations, e.g.
    after editing settings without changing a station, or to recover a
    manually-edited file. Icecast itself still needs a reload/restart to
    pick up the new file."""
    auth.require_admin(user)
    _regenerate_icecast_config()
    return {"status": "regenerated"}


@app.get("/api/users")
def api_list_users(user: dict = Depends(auth.get_current_user)):
    auth.require_admin(user)
    return db.list_users()


@app.post("/api/users")
def api_create_user(req: CreateUserRequest, user: dict = Depends(auth.get_current_user)):
    auth.require_admin(user)
    if db.get_user_by_username(req.username):
        raise HTTPException(400, "Username already taken")
    if len(req.username) < 3 or len(req.password) < 8:
        raise HTTPException(400, "Username must be 3+ chars, password 8+ chars")
    password_hash, salt = auth.hash_password(req.password)
    user_id = db.insert_user(req.username, password_hash, salt, is_admin=req.is_admin)
    return {"status": "created", "user_id": user_id}


@app.delete("/api/users/{user_id}")
def api_delete_user(user_id: int, user: dict = Depends(auth.get_current_user)):
    auth.require_admin(user)
    if user_id == user["id"]:
        raise HTTPException(400, "Cannot delete your own account")
    db.delete_user(user_id)
    return {"status": "deleted"}


# ---------- station endpoints ----------

@app.get("/api/stations")
def api_list_stations(user: dict = Depends(auth.get_current_user)):
    return manager.list_stations()


@app.post("/api/stations")
def api_create_station(req: StationCreateRequest, user: dict = Depends(auth.get_current_user)):
    existing = db.get_station(req.station_id)
    if existing:
        raise HTTPException(400, f"Station '{req.station_id}' already exists")

    settings = db.get_settings()
    if req.icecast_host is None:
        req.icecast_host = settings["icecast_internal_host"]
    if req.icecast_port is None:
        req.icecast_port = int(settings["icecast_port"])

    db.insert_station(req.model_dump())
    engine_fields = req.model_dump(exclude={"jingle_interval_tracks"})
    cfg = StationConfig(**engine_fields)
    manager.create_station(cfg)
    (MEDIA_ROOT / req.station_id).mkdir(exist_ok=True)
    _sync_auto_queue(req.station_id)
    _regenerate_icecast_config()
    return {"status": "created", "station_id": req.station_id}


@app.delete("/api/stations/{station_id}")
def api_delete_station(station_id: str, user: dict = Depends(auth.get_current_user)):
    manager.remove_station(station_id)
    db.delete_station(station_id)
    _regenerate_icecast_config()
    return {"status": "deleted"}


@app.post("/api/stations/{station_id}/start")
def api_start_station(station_id: str, user: dict = Depends(auth.get_current_user)):
    manager.start_station(station_id)
    return {"status": "started"}


@app.post("/api/stations/{station_id}/stop")
def api_stop_station(station_id: str, user: dict = Depends(auth.get_current_user)):
    manager.stop_station(station_id)
    return {"status": "stopped"}


@app.get("/api/stations/{station_id}/now-playing")
def api_now_playing(station_id: str, user: dict = Depends(auth.get_current_user)):
    return manager.now_playing(station_id) or {}


@app.put("/api/stations/{station_id}/settings")
def api_update_station_settings(station_id: str, req: StationSettingsRequest,
                                 user: dict = Depends(auth.get_current_user)):
    """Background image/color (and optional public stream URL override) shown
    on this station's public listen page -- see /listen/{station_id}."""
    if not db.get_station(station_id):
        raise HTTPException(404, "No such station")
    db.update_station_settings(station_id, req.background_url, req.background_color, req.public_stream_url)
    return {"status": "updated"}


# ---------- public listen page (no auth) ----------

@app.get("/api/public/stations/{station_id}")
def api_public_station_info(station_id: str, request: Request):
    """Everything the public player page needs, with zero auth required.
    Deliberately excludes anything sensitive (source password, host)."""
    row = db.get_station(station_id)
    if not row:
        raise HTTPException(404, "No such station")

    live_stations = {s["station_id"]: s for s in manager.list_stations()}
    live = live_stations.get(station_id)

    stream_url = row.get("public_stream_url") or ""
    if not stream_url:
        settings = db.get_settings()
        hostname = settings["public_hostname"] or request.url.hostname or row["icecast_host"]
        stream_url = f"http://{hostname}:{row['icecast_port']}{row['icecast_mount']}"

    return {
        "station_id": station_id,
        "name": row["name"],
        "running": bool(live["running"]) if live else False,
        "now_playing": live["now_playing"] if live else None,
        "background_url": row.get("background_url") or "",
        "background_color": row.get("background_color") or "#05070a",
        "stream_url": stream_url,
    }


@app.get("/listen/{station_id}")
def public_listen_page(station_id: str):
    """Serves the same static player shell for every station -- it reads
    the station_id out of the URL client-side and fetches its data from
    /api/public/stations/{id}. 404s early if the station doesn't exist so
    a bad link doesn't silently show a blank player."""
    if not db.get_station(station_id):
        raise HTTPException(404, "No such station")
    return FileResponse(static_dir / "player.html")


# ---------- upload / track endpoints ----------

@app.post("/api/stations/{station_id}/upload")
async def api_upload_track(station_id: str, file: UploadFile = File(...), user: dict = Depends(auth.get_current_user)):
    station = db.get_station(station_id)
    if not station:
        raise HTTPException(404, "No such station")

    station_dir = MEDIA_ROOT / station_id
    station_dir.mkdir(exist_ok=True)
    dest_path = station_dir / file.filename

    with dest_path.open("wb") as f:
        shutil.copyfileobj(file.file, f)

    title, artist, duration, bpm = _read_metadata(dest_path)
    track_id = db.insert_track(station_id, str(dest_path), title, artist, duration, bpm)
    _sync_auto_queue(station_id)

    return {
        "status": "uploaded",
        "track_id": track_id,
        "title": title,
        "artist": artist,
        "duration": duration,
    }


@app.post("/api/stations/{station_id}/upload-batch")
async def api_upload_tracks_batch(station_id: str, files: list[UploadFile] = File(...),
                                   user: dict = Depends(auth.get_current_user)):
    """Upload many files (multi-select or an entire folder) in one request.
    Non-audio files are skipped rather than rejecting the whole batch, so
    dropping a folder that also contains cover art or text files just works."""
    station = db.get_station(station_id)
    if not station:
        raise HTTPException(404, "No such station")

    station_dir = MEDIA_ROOT / station_id
    station_dir.mkdir(exist_ok=True)

    uploaded, skipped = [], []
    for file in files:
        if not _is_audio_filename(file.filename):
            skipped.append(file.filename)
            continue
        dest_path = station_dir / Path(file.filename).name
        with dest_path.open("wb") as f:
            shutil.copyfileobj(file.file, f)
        title, artist, duration, bpm = _read_metadata(dest_path)
        track_id = db.insert_track(station_id, str(dest_path), title, artist, duration, bpm)
        uploaded.append({"track_id": track_id, "title": title, "artist": artist, "duration": duration})

    _sync_auto_queue(station_id)
    return {"status": "uploaded", "count": len(uploaded), "tracks": uploaded, "skipped": skipped}


AUDIO_EXTENSIONS = {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".wma", ".opus"}


def _is_audio_filename(filename: str) -> bool:
    return Path(filename).suffix.lower() in AUDIO_EXTENSIONS


@app.get("/api/stations/{station_id}/tracks")
def api_list_tracks(station_id: str, user: dict = Depends(auth.get_current_user)):
    return db.list_tracks(station_id)


@app.delete("/api/tracks/{track_id}")
def api_delete_track(track_id: int, user: dict = Depends(auth.get_current_user)):
    track = db.get_track(track_id)
    db.delete_track(track_id)
    if track:
        _sync_auto_queue(track["station_id"])
    return {"status": "deleted"}


# ---------- jingle endpoints ----------

@app.post("/api/stations/{station_id}/jingles/upload")
async def api_upload_jingle(station_id: str, file: UploadFile = File(...), user: dict = Depends(auth.get_current_user)):
    station = db.get_station(station_id)
    if not station:
        raise HTTPException(404, "No such station")

    jingle_dir = MEDIA_ROOT / station_id / "jingles"
    jingle_dir.mkdir(parents=True, exist_ok=True)
    dest_path = jingle_dir / file.filename

    with dest_path.open("wb") as f:
        shutil.copyfileobj(file.file, f)

    title, _artist, duration, _bpm = _read_metadata(dest_path)
    jingle_id = db.insert_jingle(station_id, str(dest_path), title, duration)

    return {"status": "uploaded", "jingle_id": jingle_id, "title": title, "duration": duration}


@app.get("/api/stations/{station_id}/jingles")
def api_list_jingles(station_id: str, user: dict = Depends(auth.get_current_user)):
    return db.list_jingles(station_id)


@app.delete("/api/jingles/{jingle_id}")
def api_delete_jingle(jingle_id: int, user: dict = Depends(auth.get_current_user)):
    db.delete_jingle(jingle_id)
    return {"status": "deleted"}


@app.put("/api/stations/{station_id}/jingle-interval")
def api_set_jingle_interval(station_id: str, req: JingleIntervalRequest, user: dict = Depends(auth.get_current_user)):
    if req.interval_tracks < 0:
        raise HTTPException(400, "interval_tracks must be >= 0 (0 disables jingles)")
    db.set_jingle_interval(station_id, req.interval_tracks)
    return {"status": "updated", "interval_tracks": req.interval_tracks}


@app.post("/api/stations/{station_id}/jingles/{jingle_id}/play-now")
def api_play_jingle_now(station_id: str, jingle_id: int, user: dict = Depends(auth.get_current_user)):
    """Insert a specific jingle to play immediately, bypassing the interval rotation."""
    jingles = {j["id"]: j for j in db.list_jingles(station_id)}
    jingle = jingles.get(jingle_id)
    if not jingle:
        raise HTTPException(404, "No such jingle for this station")
    track = Track(id=jingle["id"], path=jingle["path"], title=jingle["title"] or "Jingle",
                  artist="", duration=jingle["duration"])
    manager.insert_priority_track(station_id, track)
    return {"status": "queued"}


def _read_metadata(path: Path):
    """Best-effort ID3/metadata read. Falls back to filename if tags are missing."""
    title, artist, duration, bpm = path.stem, "", 0.0, None
    try:
        from mutagen import File as MutagenFile
        mf = MutagenFile(path, easy=True)
        if mf is not None:
            if mf.tags:
                title = (mf.tags.get("title") or [title])[0]
                artist = (mf.tags.get("artist") or [artist])[0]
                bpm_tag = mf.tags.get("bpm")
                if bpm_tag:
                    try:
                        bpm = float(bpm_tag[0])
                    except (ValueError, TypeError):
                        pass
            if mf.info and getattr(mf.info, "length", None):
                duration = float(mf.info.length)
    except Exception:
        logger.warning("Metadata read failed for %s", path, exc_info=True)
    return title, artist, duration, bpm


# ---------- playlist endpoints ----------

@app.post("/api/playlists")
def api_create_playlist(req: PlaylistCreateRequest, user: dict = Depends(auth.get_current_user)):
    playlist_id = db.create_playlist(req.station_id, req.name)
    if req.track_ids:
        db.set_playlist_tracks(playlist_id, req.track_ids)
    return {"status": "created", "playlist_id": playlist_id}


@app.get("/api/stations/{station_id}/playlists")
def api_list_playlists(station_id: str, user: dict = Depends(auth.get_current_user)):
    return db.list_playlists(station_id)


@app.put("/api/playlists/{playlist_id}/tracks")
def api_set_playlist_tracks(playlist_id: int, track_ids: list[int], user: dict = Depends(auth.get_current_user)):
    db.set_playlist_tracks(playlist_id, track_ids)
    return {"status": "updated"}


@app.get("/api/playlists/{playlist_id}/tracks")
def api_get_playlist_tracks(playlist_id: int, user: dict = Depends(auth.get_current_user)):
    return db.get_playlist_tracks(playlist_id)


@app.post("/api/playlists/{playlist_id}/activate")
def api_activate_playlist(playlist_id: int, station_id: str, user: dict = Depends(auth.get_current_user)):
    """Immediately load a playlist into a station's live queue (manual override of schedule)."""
    tracks_raw = db.get_playlist_tracks(playlist_id)
    if not tracks_raw:
        raise HTTPException(400, "Playlist is empty")
    tracks = [
        Track(id=t["id"], path=t["path"], title=t["title"], artist=t["artist"],
              duration=t["duration"], bpm=t["bpm"])
        for t in tracks_raw
    ]
    manager.set_playlist(station_id, tracks)
    return {"status": "activated"}


# ---------- schedule endpoints ----------

@app.post("/api/schedule")
def api_add_schedule_block(req: ScheduleBlockRequest, user: dict = Depends(auth.get_current_user)):
    block_id = db.add_schedule_block(
        req.station_id, req.playlist_id, req.day_of_week, req.start_time, req.end_time
    )
    return {"status": "created", "block_id": block_id}


@app.get("/api/stations/{station_id}/schedule")
def api_list_schedule(station_id: str, user: dict = Depends(auth.get_current_user)):
    return db.list_schedule(station_id)


@app.delete("/api/schedule/{block_id}")
def api_delete_schedule_block(block_id: int, user: dict = Depends(auth.get_current_user)):
    db.delete_schedule_block(block_id)
    return {"status": "deleted"}


# ---------- static dashboard ----------
app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")
