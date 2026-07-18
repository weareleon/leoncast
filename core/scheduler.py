"""
leonCAST - Scheduler
Background thread that checks, per station, which schedule block is
active right now and pushes the corresponding playlist into that
station's live queue if it isn't already loaded. ErsatzTV-lite, basically.
"""

import logging
import threading
import time
from datetime import datetime

from data import db
from core.station_engine import Track
from core.station_manager import manager

logger = logging.getLogger("leoncast.scheduler")

CHECK_INTERVAL_SECONDS = 30


class Scheduler:
    def __init__(self):
        self._running = False
        self._thread: threading.Thread | None = None
        self._active_playlist: dict[str, int] = {}  # station_id -> playlist_id currently loaded

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info("Scheduler started")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self):
        while self._running:
            try:
                self._tick()
            except Exception:
                logger.exception("Scheduler tick failed")
            time.sleep(CHECK_INTERVAL_SECONDS)

    def _tick(self):
        now = datetime.now()
        current_day = now.weekday()  # 0=Monday
        current_time = now.strftime("%H:%M")

        for station in db.list_stations():
            station_id = station["station_id"]
            blocks = db.list_schedule(station_id)
            match = self._find_matching_block(blocks, current_day, current_time)
            if not match:
                continue

            playlist_id = match["playlist_id"]
            if self._active_playlist.get(station_id) == playlist_id:
                continue  # already loaded, nothing to do

            tracks_raw = db.get_playlist_tracks(playlist_id)
            if not tracks_raw:
                logger.warning("Playlist %s for station %s is empty, skipping", playlist_id, station_id)
                continue

            tracks = [
                Track(id=t["id"], path=t["path"], title=t["title"],
                      artist=t["artist"], duration=t["duration"], bpm=t["bpm"])
                for t in tracks_raw
            ]
            manager.set_playlist(station_id, tracks)
            self._active_playlist[station_id] = playlist_id
            logger.info("Station %s switched to playlist %s (scheduled block)", station_id, playlist_id)

    @staticmethod
    def _find_matching_block(blocks: list[dict], day: int, hhmm: str) -> dict | None:
        for b in blocks:
            if b["day_of_week"] not in (day, -1):
                continue
            if b["start_time"] <= hhmm < b["end_time"]:
                return b
        return None


scheduler = Scheduler()
