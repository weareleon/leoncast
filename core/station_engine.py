"""
leonCAST - Station Engine
Manages a single radio station: its play queue, crossfade streaming process,
and connection to an Icecast mount point.

Design:
- Each station runs its own long-lived ffmpeg process.
- Instead of trying to dynamically inject tracks into a single ffmpeg filter_complex
  (hard to do live), we run ffmpeg in short "segment" mode: build a crossfaded
  segment of N upcoming tracks, stream it, then rebuild the next segment.
  This keeps the crossfade quality of acrossfade while allowing the queue
  to change dynamically (skip, reorder, insert requests) between segments.
- A background thread continuously keeps 1 segment "ahead" so playback never
  gaps between segments.
"""

import subprocess
import threading
import queue
import time
import json
import logging
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("leoncast.engine")

DEFAULT_CROSSFADE_SECONDS = 3.0
SEGMENT_TRACK_COUNT = 4  # how many tracks to batch into one crossfaded ffmpeg segment


@dataclass
class Track:
    id: int
    path: str
    title: str = ""
    artist: str = ""
    duration: float = 0.0
    bpm: Optional[float] = None


@dataclass
class StationConfig:
    station_id: str
    name: str
    icecast_host: str
    icecast_port: int
    icecast_mount: str
    icecast_source_password: str
    bitrate_kbps: int = 128
    crossfade_seconds: float = DEFAULT_CROSSFADE_SECONDS
    sample_rate: int = 44100


class TrackQueue:
    """Thread-safe playlist/queue for a station.

    Pulls from an ordered playlist by default, but supports ad-hoc
    insertion (e.g. a request or scheduled jingle) at the front of the queue.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._playlist: list[Track] = []
        self._playlist_pos = 0
        self._priority_queue: "queue.Queue[Track]" = queue.Queue()

    def set_playlist(self, tracks: list[Track]):
        with self._lock:
            self._playlist = tracks
            self._playlist_pos = 0

    def insert_priority(self, track: Track):
        """Insert a track to play next, ahead of the normal playlist (e.g. request/jingle)."""
        self._priority_queue.put(track)

    def build_segment(self, n: int) -> tuple[list[Track], int]:
        """Build the next up-to-n tracks for a streaming segment.

        Priority tracks (jingles, requests) are drained first and consumed
        for real -- they only ever play once per insertion. Any remaining
        slots are filled from the ordered playlist via a non-destructive
        peek, since the playlist cursor is only advanced afterward, once
        the caller knows the segment actually got streamed.

        Returns (segment_tracks, playlist_track_count) so the caller knows
        how far to advance the playlist cursor -- priority tracks are
        already consumed and must not be counted.
        """
        out: list[Track] = []
        while len(out) < n:
            try:
                out.append(self._priority_queue.get_nowait())
            except queue.Empty:
                break

        remaining = n - len(out)
        playlist_part = self.peek_next_n(remaining) if remaining > 0 else []
        out.extend(playlist_part)
        return out, len(playlist_part)

    def next_track(self) -> Optional[Track]:
        """Kept for callers that want a single track at a time. Note:
        StationEngine uses build_segment() instead for segment streaming."""
        if not self._priority_queue.empty():
            try:
                return self._priority_queue.get_nowait()
            except queue.Empty:
                pass

        with self._lock:
            if not self._playlist:
                return None
            track = self._playlist[self._playlist_pos]
            self._playlist_pos = (self._playlist_pos + 1) % len(self._playlist)
            return track

    def peek_next_n(self, n: int) -> list[Track]:
        """Non-destructive peek used for building segments; does not consume queue."""
        with self._lock:
            if not self._playlist:
                return []
            out = []
            pos = self._playlist_pos
            for _ in range(n):
                out.append(self._playlist[pos])
                pos = (pos + 1) % len(self._playlist)
            return out

    def advance(self, n: int):
        with self._lock:
            if self._playlist:
                self._playlist_pos = (self._playlist_pos + n) % len(self._playlist)


class StationEngine:
    """Owns the ffmpeg process lifecycle for one station and streams
    crossfaded segments continuously to its Icecast mount.

    Two kinds of ffmpeg processes are involved, and it matters that they
    are NOT the same process:
      - One persistent "encoder" process holds the single TCP connection
        to Icecast open for as long as the station is running. It reads
        raw PCM from its stdin and re-encodes/streams that to Icecast.
      - Many short-lived "decoder" processes, one per segment, each just
        decode + crossfade a batch of tracks to raw PCM and pipe the
        result into the encoder's stdin, then exit.
    Earlier this used a single ffmpeg process per segment that both
    decoded/crossfaded AND connected to Icecast directly -- which meant
    the Icecast connection itself was torn down and re-established every
    SEGMENT_TRACK_COUNT tracks, causing an audible dropout/reconnect on
    every segment boundary. Splitting decode from encode means Icecast
    only sees one continuous connection for the station's entire runtime.
    """

    def __init__(self, config: StationConfig, track_queue: TrackQueue):
        self.config = config
        self.track_queue = track_queue
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._encoder_process: Optional[subprocess.Popen] = None
        self._current_process: Optional[subprocess.Popen] = None
        self._now_playing: Optional[Track] = None
        self._status_lock = threading.Lock()
        self._tracks_played = 0
        self._write_lock = threading.Lock()

    # ---------- public control ----------

    def start(self):
        if self._running:
            logger.warning("Station %s already running", self.config.station_id)
            return
        self._running = True
        self._encoder_process = self._spawn_encoder()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info("Station %s started", self.config.station_id)

    def stop(self):
        self._running = False
        # Kill the in-flight decoder first so a blocking stdout.read() in
        # the run loop returns immediately instead of waiting out the
        # rest of the current track.
        if self._current_process and self._current_process.poll() is None:
            self._current_process.terminate()
        if self._thread:
            self._thread.join(timeout=5)
        self._close_encoder()
        logger.info("Station %s stopped", self.config.station_id)

    def now_playing(self) -> Optional[dict]:
        with self._status_lock:
            if not self._now_playing:
                return None
            return {
                "id": self._now_playing.id,
                "title": self._now_playing.title,
                "artist": self._now_playing.artist,
            }

    # ---------- internals ----------

    def _icecast_url(self) -> str:
        c = self.config
        return f"icecast://source:{c.icecast_source_password}@{c.icecast_host}:{c.icecast_port}{c.icecast_mount}"

    def _spawn_encoder(self) -> subprocess.Popen:
        """Start the single long-lived process that owns the Icecast
        connection for as long as the station is running. It just
        re-encodes whatever raw PCM it's fed on stdin -- it never touches
        track files directly, so it never needs to restart between tracks."""
        cfg = self.config
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
            "-f", "s16le", "-ar", str(cfg.sample_rate), "-ac", "2",
            "-re", "-i", "pipe:0",
            "-b:a", f"{cfg.bitrate_kbps}k",
            "-content_type", "audio/mpeg",
            "-f", "mp3",
            self._icecast_url(),
        ]
        logger.info("Station %s connecting encoder to Icecast", cfg.station_id)
        return subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    def _close_encoder(self):
        proc, self._encoder_process = self._encoder_process, None
        if not proc:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        if proc.poll() is None:
            proc.terminate()

    def _restart_encoder(self):
        """Icecast connection was lost (server restarted, wrong password
        fixed, network hiccup, etc). Tear down and reconnect once, rather
        than tearing down for every routine segment change."""
        logger.warning("Station %s encoder connection dropped, reconnecting", self.config.station_id)
        self._close_encoder()
        self._encoder_process = self._spawn_encoder()

    def _run_loop(self):
        while self._running:
            segment_tracks, playlist_used = self.track_queue.build_segment(SEGMENT_TRACK_COUNT)
            if not segment_tracks:
                logger.warning("Station %s has no tracks queued, retrying in 5s", self.config.station_id)
                time.sleep(5)
                continue

            try:
                self._decode_segment_into_encoder(segment_tracks)
                self.track_queue.advance(playlist_used)
                with self._status_lock:
                    self._tracks_played += len(segment_tracks)
            except (BrokenPipeError, OSError):
                # The encoder's connection to Icecast died -- reconnect and
                # retry this exact segment rather than dropping the tracks.
                if self._running:
                    self._restart_encoder()
                    time.sleep(1)
            except Exception:
                logger.exception("Station %s segment failed, backing off", self.config.station_id)
                time.sleep(3)

    def tracks_played(self) -> int:
        """Running count of tracks streamed since this engine started. Used
        by JingleService to decide when to insert the next jingle."""
        with self._status_lock:
            return self._tracks_played

    def _decode_segment_into_encoder(self, tracks: list[Track]):
        """Decode + crossfade this segment's tracks to raw PCM (short-lived
        process, no network involved) and stream those bytes straight into
        the persistent encoder's stdin as they're produced. The encoder's
        Icecast connection is untouched by this -- only the source audio
        changes underneath it."""
        cfg = self.config
        cf = cfg.crossfade_seconds

        inputs = []
        for t in tracks:
            inputs += ["-i", t.path]

        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning"]
        cmd += inputs

        if len(tracks) == 1:
            cmd += ["-filter_complex", "[0:a]anull[aout]", "-map", "[aout]"]
        else:
            # chain acrossfade filters: (((0 x 1) x 2) x 3) ...
            chain = "[0:a][1:a]acrossfade=d={}:c1=tri:c2=tri[a01]".format(cf)
            last_label = "a01"
            for idx in range(2, len(tracks)):
                new_label = f"a0{idx}"
                chain += f";[{last_label}][{idx}:a]acrossfade=d={cf}:c1=tri:c2=tri[{new_label}]"
                last_label = new_label
            cmd += ["-filter_complex", chain, "-map", f"[{last_label}]"]

        cmd += ["-f", "s16le", "-ar", str(cfg.sample_rate), "-ac", "2", "pipe:1"]

        logger.debug("Station %s decode cmd: %s", cfg.station_id, " ".join(shlex.quote(c) for c in cmd))

        with self._status_lock:
            self._now_playing = tracks[0]
        self._track_progress_watch(tracks)

        decode_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self._current_process = decode_proc
        try:
            while True:
                chunk = decode_proc.stdout.read(65536)
                if not chunk:
                    break
                encoder = self._encoder_process
                if encoder is None or encoder.stdin is None or encoder.poll() is not None:
                    raise BrokenPipeError("encoder is not connected")
                with self._write_lock:
                    encoder.stdin.write(chunk)
        finally:
            decode_proc.stdout.close()
            _stdout, stderr = decode_proc.communicate()
            if decode_proc.returncode not in (0, None) and decode_proc.returncode != 0 and self._running:
                err_tail = stderr.decode(errors="replace")[-500:] if stderr else ""
                raise RuntimeError(
                    f"decode failed with code {decode_proc.returncode} "
                    f"for station {cfg.station_id}: {err_tail}"
                )


    def _track_progress_watch(self, tracks: list[Track]):
        """Spawn a lightweight timer thread to advance now_playing within a segment."""
        def watcher():
            for idx, t in enumerate(tracks[:-1]):
                if not t.duration:
                    return  # can't estimate without duration metadata
                sleep_for = max(t.duration - self.config.crossfade_seconds, 0.5)
                time.sleep(sleep_for)
                if not self._running:
                    return
                with self._status_lock:
                    self._now_playing = tracks[idx + 1]

        threading.Thread(target=watcher, daemon=True).start()
