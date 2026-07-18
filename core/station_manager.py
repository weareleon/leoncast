"""
leonCAST - Station Manager
Owns the lifecycle of all StationEngine instances, and is the single
point the API layer talks to for station control, queue/playlist
manipulation, and status queries.
"""

import logging
import threading
from typing import Optional

from core.station_engine import StationEngine, StationConfig, TrackQueue, Track

logger = logging.getLogger("leoncast.manager")


class StationManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._engines: dict[str, StationEngine] = {}
        self._queues: dict[str, TrackQueue] = {}
        self._configs: dict[str, StationConfig] = {}

    # ---------- station lifecycle ----------

    def create_station(self, config: StationConfig) -> StationEngine:
        with self._lock:
            if config.station_id in self._engines:
                raise ValueError(f"Station '{config.station_id}' already exists")
            tq = TrackQueue()
            engine = StationEngine(config, tq)
            self._engines[config.station_id] = engine
            self._queues[config.station_id] = tq
            self._configs[config.station_id] = config
            logger.info("Created station %s (%s)", config.station_id, config.name)
            return engine

    def remove_station(self, station_id: str):
        with self._lock:
            engine = self._engines.pop(station_id, None)
            self._queues.pop(station_id, None)
            self._configs.pop(station_id, None)
        if engine:
            engine.stop()
            logger.info("Removed station %s", station_id)

    def start_station(self, station_id: str):
        engine = self._get_engine(station_id)
        engine.start()

    def stop_station(self, station_id: str):
        engine = self._get_engine(station_id)
        engine.stop()

    def start_all(self):
        with self._lock:
            engines = list(self._engines.values())
        for e in engines:
            e.start()

    def stop_all(self):
        with self._lock:
            engines = list(self._engines.values())
        for e in engines:
            e.stop()

    # ---------- playlist / queue control ----------

    def set_playlist(self, station_id: str, tracks: list[Track]):
        tq = self._get_queue(station_id)
        tq.set_playlist(tracks)

    def insert_priority_track(self, station_id: str, track: Track):
        tq = self._get_queue(station_id)
        tq.insert_priority(track)

    # ---------- status ----------

    def now_playing(self, station_id: str) -> Optional[dict]:
        engine = self._get_engine(station_id)
        return engine.now_playing()

    def tracks_played(self, station_id: str) -> int:
        engine = self._get_engine(station_id)
        return engine.tracks_played()

    def list_stations(self) -> list[dict]:
        with self._lock:
            out = []
            for sid, cfg in self._configs.items():
                engine = self._engines[sid]
                out.append({
                    "station_id": sid,
                    "name": cfg.name,
                    "mount": cfg.icecast_mount,
                    "running": engine._running,
                    "now_playing": engine.now_playing(),
                })
            return out

    def get_config(self, station_id: str) -> StationConfig:
        with self._lock:
            if station_id not in self._configs:
                raise KeyError(f"No such station: {station_id}")
            return self._configs[station_id]

    # ---------- internals ----------

    def _get_engine(self, station_id: str) -> StationEngine:
        with self._lock:
            if station_id not in self._engines:
                raise KeyError(f"No such station: {station_id}")
            return self._engines[station_id]

    def _get_queue(self, station_id: str) -> TrackQueue:
        with self._lock:
            if station_id not in self._queues:
                raise KeyError(f"No such station: {station_id}")
            return self._queues[station_id]


# Single process-wide instance used by the API layer
manager = StationManager()
