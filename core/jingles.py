"""
leonCAST - Jingle Service
Background thread that inserts a station's jingles into its live queue
on a rotating basis, based on a per-station "every N tracks" interval.

Jingles are inserted via the same priority-queue path used for one-off
requests (StationEngine's TrackQueue.build_segment drains this queue
first), so a jingle preempts the very next segment build without
disturbing the playlist cursor.
"""

import logging
import random
import threading
import time

from data import db
from core.station_engine import Track
from core.station_manager import manager

logger = logging.getLogger("leoncast.jingles")

CHECK_INTERVAL_SECONDS = 10


class JingleService:
    def __init__(self):
        self._running = False
        self._thread: threading.Thread | None = None
        # tracks_played count at which each station last got a jingle inserted
        self._last_jingle_at: dict[str, int] = {}
        # per-station shuffled rotation order, refilled when exhausted
        self._rotation: dict[str, list[dict]] = {}

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info("JingleService started")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self):
        while self._running:
            try:
                self._tick()
            except Exception:
                logger.exception("JingleService tick failed")
            time.sleep(CHECK_INTERVAL_SECONDS)

    def _tick(self):
        for station in db.list_stations():
            station_id = station["station_id"]
            interval = station["jingle_interval_tracks"]
            if not interval or interval <= 0:
                continue

            try:
                played = manager.tracks_played(station_id)
            except KeyError:
                continue  # engine not registered yet

            last = self._last_jingle_at.get(station_id, 0)
            if played - last < interval:
                continue

            jingle = self._next_jingle(station_id)
            if jingle is None:
                continue  # no jingles uploaded for this station yet

            track = Track(
                id=jingle["id"], path=jingle["path"], title=jingle["title"] or "Jingle",
                artist="", duration=jingle["duration"],
            )
            manager.insert_priority_track(station_id, track)
            self._last_jingle_at[station_id] = played
            logger.info("Station %s: queued jingle '%s' after %d tracks", station_id, track.title, played)

    def _next_jingle(self, station_id: str) -> dict | None:
        """Pop the next jingle from this station's shuffled rotation,
        reshuffling from the DB list whenever it runs out."""
        rotation = self._rotation.get(station_id) or []
        if not rotation:
            jingles = db.list_jingles(station_id)
            if not jingles:
                return None
            random.shuffle(jingles)
            rotation = jingles
        jingle = rotation.pop()
        self._rotation[station_id] = rotation
        return jingle


jingle_service = JingleService()
