"""
leonCAST - Database layer
Simple SQLite persistence for stations, tracks, playlists, and schedule blocks.
No ORM — this project is meant to stay small and dependency-light.
"""

import sqlite3
import time
from pathlib import Path
from contextlib import contextmanager

DB_PATH = Path(__file__).parent / "leoncast.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS stations (
    station_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    icecast_host TEXT NOT NULL,
    icecast_port INTEGER NOT NULL,
    icecast_mount TEXT NOT NULL,
    icecast_source_password TEXT NOT NULL,
    bitrate_kbps INTEGER DEFAULT 128,
    crossfade_seconds REAL DEFAULT 3.0,
    sample_rate INTEGER DEFAULT 44100,
    jingle_interval_tracks INTEGER DEFAULT 0, -- 0 = jingles disabled
    background_url TEXT DEFAULT '',           -- public player page background image
    background_color TEXT DEFAULT '#05070a',  -- public player page fallback/base color
    public_stream_url TEXT DEFAULT '',        -- override if Icecast isn't reachable at icecast_host:icecast_port from listeners
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS jingles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id TEXT NOT NULL,
    path TEXT NOT NULL,
    title TEXT DEFAULT '',
    duration REAL DEFAULT 0,
    uploaded_at REAL NOT NULL,
    FOREIGN KEY (station_id) REFERENCES stations(station_id)
);

CREATE TABLE IF NOT EXISTS tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id TEXT NOT NULL,
    path TEXT NOT NULL,
    title TEXT DEFAULT '',
    artist TEXT DEFAULT '',
    duration REAL DEFAULT 0,
    bpm REAL,
    uploaded_at REAL NOT NULL,
    FOREIGN KEY (station_id) REFERENCES stations(station_id)
);

CREATE TABLE IF NOT EXISTS playlists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id TEXT NOT NULL,
    name TEXT NOT NULL,
    created_at REAL NOT NULL,
    FOREIGN KEY (station_id) REFERENCES stations(station_id)
);

CREATE TABLE IF NOT EXISTS playlist_tracks (
    playlist_id INTEGER NOT NULL,
    track_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    FOREIGN KEY (playlist_id) REFERENCES playlists(id),
    FOREIGN KEY (track_id) REFERENCES tracks(id)
);

CREATE TABLE IF NOT EXISTS schedule_blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id TEXT NOT NULL,
    playlist_id INTEGER NOT NULL,
    day_of_week INTEGER NOT NULL, -- 0=Monday ... 6=Sunday, -1 = every day
    start_time TEXT NOT NULL,     -- 'HH:MM' 24hr
    end_time TEXT NOT NULL,
    FOREIGN KEY (station_id) REFERENCES stations(station_id),
    FOREIGN KEY (playlist_id) REFERENCES playlists(id)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
"""


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate_add_missing_columns(conn)


def _migrate_add_missing_columns(conn):
    """Lightweight migration for DBs created before background/public-page
    settings existed. SQLite has no 'ADD COLUMN IF NOT EXISTS', so check
    first."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(stations)").fetchall()}
    additions = {
        "background_url": "TEXT DEFAULT ''",
        "background_color": "TEXT DEFAULT '#05070a'",
        "public_stream_url": "TEXT DEFAULT ''",
    }
    for col, ddl in additions.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE stations ADD COLUMN {col} {ddl}")


# ---------- global settings ----------

DEFAULT_SETTINGS = {
    # Public hostname/domain listeners and the dashboard's own browser use to
    # reach this box -- e.g. "radio.example.com" or an IP. Used to build
    # public listen-page stream URLs and shown as the base for share links.
    # Leave blank to fall back to guessing from the incoming request.
    "public_hostname": "",
    # Internal host ffmpeg source-connects to when pushing audio to Icecast.
    # Usually "localhost" if Icecast runs on the same machine/container.
    "icecast_internal_host": "localhost",
    # Port Icecast listens on, both for the internal source connection and
    # (unless a station overrides it) the public stream URL.
    "icecast_port": "8000",
    "icecast_admin_password": "changeme",
    "icecast_default_source_password": "changeme",
}


def get_settings() -> dict:
    with get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    stored = {r["key"]: r["value"] for r in rows}
    return {**DEFAULT_SETTINGS, **stored}


def update_settings(values: dict):
    with get_conn() as conn:
        for key, value in values.items():
            if key not in DEFAULT_SETTINGS:
                continue
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )


# ---------- stations ----------

def insert_station(cfg: dict):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO stations
               (station_id, name, icecast_host, icecast_port, icecast_mount,
                icecast_source_password, bitrate_kbps, crossfade_seconds, sample_rate,
                jingle_interval_tracks, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (cfg["station_id"], cfg["name"], cfg["icecast_host"], cfg["icecast_port"],
             cfg["icecast_mount"], cfg["icecast_source_password"], cfg.get("bitrate_kbps", 128),
             cfg.get("crossfade_seconds", 3.0), cfg.get("sample_rate", 44100),
             cfg.get("jingle_interval_tracks", 0), time.time()),
        )


def set_jingle_interval(station_id: str, interval_tracks: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE stations SET jingle_interval_tracks = ? WHERE station_id = ?",
            (interval_tracks, station_id),
        )


def update_station_settings(station_id: str, background_url: str = "",
                             background_color: str = "#05070a", public_stream_url: str = ""):
    with get_conn() as conn:
        conn.execute(
            """UPDATE stations SET background_url = ?, background_color = ?, public_stream_url = ?
               WHERE station_id = ?""",
            (background_url, background_color, public_stream_url, station_id),
        )


def delete_station(station_id: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM stations WHERE station_id = ?", (station_id,))


def list_stations() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM stations").fetchall()
        return [dict(r) for r in rows]


def get_station(station_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM stations WHERE station_id = ?", (station_id,)).fetchone()
        return dict(row) if row else None


# ---------- tracks ----------

def insert_track(station_id: str, path: str, title: str = "", artist: str = "",
                  duration: float = 0.0, bpm: float | None = None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO tracks (station_id, path, title, artist, duration, bpm, uploaded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (station_id, path, title, artist, duration, bpm, time.time()),
        )
        return cur.lastrowid


def list_tracks(station_id: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM tracks WHERE station_id = ? ORDER BY uploaded_at ASC", (station_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_track(track_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
        return dict(row) if row else None


def delete_track(track_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM tracks WHERE id = ?", (track_id,))


# ---------- playlists ----------

def create_playlist(station_id: str, name: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO playlists (station_id, name, created_at) VALUES (?, ?, ?)",
            (station_id, name, time.time()),
        )
        return cur.lastrowid


def set_playlist_tracks(playlist_id: int, track_ids: list[int]):
    with get_conn() as conn:
        conn.execute("DELETE FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,))
        conn.executemany(
            "INSERT INTO playlist_tracks (playlist_id, track_id, position) VALUES (?, ?, ?)",
            [(playlist_id, tid, i) for i, tid in enumerate(track_ids)],
        )


def get_playlist_tracks(playlist_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT t.* FROM tracks t
               JOIN playlist_tracks pt ON pt.track_id = t.id
               WHERE pt.playlist_id = ?
               ORDER BY pt.position ASC""",
            (playlist_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def list_playlists(station_id: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM playlists WHERE station_id = ?", (station_id,)).fetchall()
        return [dict(r) for r in rows]


# ---------- schedule ----------

def add_schedule_block(station_id: str, playlist_id: int, day_of_week: int,
                        start_time: str, end_time: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO schedule_blocks (station_id, playlist_id, day_of_week, start_time, end_time)
               VALUES (?, ?, ?, ?, ?)""",
            (station_id, playlist_id, day_of_week, start_time, end_time),
        )
        return cur.lastrowid


def list_schedule(station_id: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM schedule_blocks WHERE station_id = ?", (station_id,)).fetchall()
        return [dict(r) for r in rows]


def delete_schedule_block(block_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM schedule_blocks WHERE id = ?", (block_id,))


# ---------- jingles ----------

def insert_jingle(station_id: str, path: str, title: str = "", duration: float = 0.0) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO jingles (station_id, path, title, duration, uploaded_at) VALUES (?, ?, ?, ?, ?)",
            (station_id, path, title, duration, time.time()),
        )
        return cur.lastrowid


def list_jingles(station_id: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM jingles WHERE station_id = ?", (station_id,)).fetchall()
        return [dict(r) for r in rows]


def delete_jingle(jingle_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM jingles WHERE id = ?", (jingle_id,))


# ---------- users / auth ----------

def any_users_exist() -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()
        return row["c"] > 0


def insert_user(username: str, password_hash: str, salt: str, is_admin: bool) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, salt, is_admin, created_at) VALUES (?, ?, ?, ?, ?)",
            (username, password_hash, salt, int(is_admin), time.time()),
        )
        return cur.lastrowid


def get_user_by_username(username: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return dict(row) if row else None


def get_user_by_id(user_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def list_users() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT id, username, is_admin, created_at FROM users").fetchall()
        return [dict(r) for r in rows]


def delete_user(user_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def create_session(token: str, user_id: int, expires_at: float):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token, user_id, time.time(), expires_at),
        )


def get_session(token: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE token = ?", (token,)).fetchone()
        return dict(row) if row else None


def delete_session(token: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
