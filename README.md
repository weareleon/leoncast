# leonCAST

A lightweight, self-hosted alternative to AzuraCast. Multi-station internet
radio with BPM-aware crossfading, uploads, playlists, scheduling, jingles,
and shuffle — without the bloat, and without needing Docker.

leonCAST is just Python + ffmpeg + Icecast running directly on the host.
That makes it a good fit for low-spec boxes, old laptops, and VPSes where
Docker either isn't installed or isn't worth the overhead — there's no
container runtime, no image to pull, nothing to build. Clone it, `pip
install`, and run.

## Stack

- **Python** — station engine, API, scheduler
- **ffmpeg** — crossfaded segment rendering (`acrossfade` filter chains)
- **Icecast** — actual stream distribution
- **FastAPI** — REST API
- **SQLite** — stations / tracks / playlists / schedule / users / settings
- **Vanilla JS dashboard** — Fluent Design–inspired control room UI

## Features

- **Multi-station** — run as many stations as your box can handle ffmpeg
  processes for, each with its own Icecast mount, bitrate, sample rate,
  and crossfade length.
- **BPM-aware crossfading** — tracks are crossfaded into each other with
  ffmpeg's `acrossfade` filter; BPM/duration/title/artist are read via
  `mutagen` on upload.
- **Shuffle by default** — a station's auto-queue (everything in its
  upload library) plays in random order and reshuffles every time it
  loops back to the start, instead of always playing uploads in the order
  they were added. Manually built playlists and scheduled blocks keep
  whatever order you put them in. Shuffle can be turned off per station
  from its settings.
- **Playlists & scheduling** — build named playlists from a station's
  track library, activate one manually at any time, or let the scheduler
  swap playlists in automatically based on day-of-week + time-range
  blocks (an ErsatzTV-lite for audio).
- **Jingles** — upload jingles per station and set a "play every N
  tracks" interval; jingles are inserted into the live queue without
  disturbing the playlist's position, or can be triggered on demand.
- **Per-station public listen page** — every station gets a shareable
  `/listen/{station_id}` link with no account needed to listen, a
  customizable background image/color, and an optional public stream
  URL override for when Icecast isn't reachable at the same hostname as
  the dashboard.
- **Customizable login screen** — the background behind the login/setup
  screen can be set to an image URL and/or color from the dashboard's
  global Settings tab, so it isn't the same for every install.
- **Users & auth** — login-only (no public signup); the first account
  created on first boot becomes an admin, and every account after that is
  created by an admin from the Users tab. Sessions are opaque
  server-side tokens, not JWTs, so revoking one is just deleting a row.
- **Isolated libraries** — every station's tracks and jingles are scoped
  strictly by `station_id`; nothing is shared or visible across stations.

## How it works

Each station runs its own background thread (`StationEngine`) that:

1. Peeks the next few tracks off that station's `TrackQueue`
2. Builds an ffmpeg `filter_complex` chain of `acrossfade` filters across them
3. Streams the resulting segment straight to the station's Icecast mount
4. Advances the queue and repeats, so playback (and crossfading) never stops

`StationManager` owns all `StationEngine` instances. The `Scheduler` runs a
30-second tick loop, checks each station's schedule blocks, and swaps in
the right playlist automatically. The `JingleService` runs its own loop
that inserts a station's next jingle once it's played enough tracks since
the last one.

## Running it

leonCAST runs directly on the host — no Docker required. All you need is
Python 3.11+, ffmpeg, and Icecast.

### 1. Install system dependencies

**Debian / Ubuntu:**

```bash
sudo apt update
sudo apt install -y ffmpeg icecast2
```

(When the Icecast installer asks whether to configure it now, you can say
no — leonCAST generates its own `data/icecast.xml` for you.)

**macOS (Homebrew):**

```bash
brew install ffmpeg icecast
```

**Other platforms:** install ffmpeg and Icecast however your distro
prefers — leonCAST just shells out to the `ffmpeg` and `icecast2` binaries
on your `PATH`.

### 2. Install leonCAST

```bash
git clone <this repo>
cd leoncast

python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

(On some Linux distros with an externally-managed Python you may need
`pip install -r requirements.txt --break-system-packages` instead of a
venv.)

### 3. First run

```bash
uvicorn api.main:app --host 0.0.0.0 --port 8080
```

This creates `data/leoncast.db` (SQLite) and `media/` (uploaded audio) on
first startup. Open `http://localhost:8080` — you'll land on a "create
admin account" screen since no users exist yet. Once that account is
created, go to **Settings** and fill in your Icecast admin/source
passwords, then hit **Regenerate icecast.xml**.

### 4. Start Icecast

```bash
icecast2 -c data/icecast.xml
```

Re-run this (or reload Icecast) any time you add/remove a station, since
`write_icecast_config()` regenerates `data/icecast.xml` with each
station's mount point.

### 5. Create a station

From the dashboard, click **+ New station**, give it a mount point (e.g.
`/mystation`) and a source password matching what's in `data/icecast.xml`,
then upload tracks — the station starts auto-queuing and shuffling them
as soon as there's at least one track.

### Running it long-term

leonCAST doesn't daemonize itself — for a machine that stays on, run it
under whatever process supervisor you'd normally use (`systemd`, `tmux`,
`screen`, `supervisord`, etc.) so it survives reboots and crashes. A
minimal systemd unit just needs `ExecStart` pointing at the `uvicorn`
command above with your venv's Python on `PATH`.

## API quick reference

| Method | Path | What |
|---|---|---|
| GET | `/api/auth/status` | first-run check (no auth) |
| POST | `/api/auth/setup` | create the first admin account (no auth, one-time) |
| POST | `/api/auth/login` \| `/logout` | session login/logout |
| GET | `/api/auth/me` | current user |
| GET/POST | `/api/users` | list / create users (admin) |
| DELETE | `/api/users/{id}` | remove a user (admin) |
| GET/PUT | `/api/settings` | global networking / Icecast / login-background settings |
| POST | `/api/settings/regenerate-icecast-config` | rewrite `data/icecast.xml` |
| GET/POST | `/api/stations` | list / create stations |
| DELETE | `/api/stations/{id}` | remove a station |
| POST | `/api/stations/{id}/start` \| `/stop` | go live / take offline |
| GET | `/api/stations/{id}/now-playing` | current track |
| PUT | `/api/stations/{id}/settings` | background, shuffle, public stream URL override |
| GET | `/api/public/stations/{id}` | public listen-page data (no auth) |
| GET | `/listen/{id}` | public listen page (no auth) |
| POST | `/api/stations/{id}/upload` \| `/upload-batch` | upload track(s) (multipart) |
| GET | `/api/stations/{id}/tracks` | list uploaded tracks |
| DELETE | `/api/tracks/{id}` | remove a track |
| POST | `/api/stations/{id}/jingles/upload` | upload a jingle |
| GET | `/api/stations/{id}/jingles` | list jingles |
| DELETE | `/api/jingles/{id}` | remove a jingle |
| PUT | `/api/stations/{id}/jingle-interval` | set "every N tracks" jingle interval |
| POST | `/api/stations/{id}/jingles/{jid}/play-now` | trigger a jingle immediately |
| POST | `/api/playlists` | create a playlist |
| GET | `/api/stations/{id}/playlists` | list a station's playlists |
| PUT/GET | `/api/playlists/{id}/tracks` | set / get a playlist's track order |
| POST | `/api/playlists/{id}/activate?station_id=` | load a playlist live now |
| POST | `/api/schedule` | add a day/time → playlist block |
| GET | `/api/stations/{id}/schedule` | list a station's schedule blocks |
| DELETE | `/api/schedule/{id}` | remove a schedule block |

## Auth

Login only — there's no public signup. On first startup, `/api/auth/status`
reports `needs_setup: true` and the dashboard shows a "create admin account"
screen instead of a login form. Once that account exists, `needs_setup`
flips to `false` permanently, and every account after that has to be
created by an admin from the Users tab in the dashboard.

Sessions are opaque tokens stored server-side in SQLite (not JWTs), sent as
`Authorization: Bearer <token>`, so revoking a session is just deleting a
row. Passwords are PBKDF2-HMAC-SHA256 hashed with a per-user salt.

## Libraries

Every station's tracks and jingles are scoped strictly by `station_id` —
there's no shared/global library. Click into a station from the dashboard
to see and manage its own track list and jingle rotation; nothing is
visible across stations.

## Theme

Black/green, matches the leonCAST logo. Logo is pulled directly from
`https://files.catbox.moe/0jxcd1.png` in the dashboard HTML — swap that URL
in `static/index.html` if you move it somewhere more permanent later.

## Security notes before you deploy

- **Change the default passwords.** The Icecast admin/source passwords
  default to `changeme` (and the relay password in the generated
  `icecast.xml` defaults to `hackme`). Set real values from the dashboard's
  global settings (or `core/icecast_config.py` if you use relays) before
  exposing the stream publicly.
- **CORS is wide open (`allow_origins=["*"]`)** in `api/main.py` for ease of
  local development. Lock this down to your actual dashboard origin if you
  deploy leonCAST somewhere reachable from the internet.
- Session tokens are bearer tokens with no CSRF protection needed (no
  cookies), but make sure you serve the dashboard over HTTPS in production
  so tokens aren't sent in the clear.

## Notes

- Every time you add/remove a station, `write_icecast_config()` regenerates
  `data/icecast.xml` with the right mount points — reload Icecast after.
- BPM falls back to `null` if a track isn't tagged with one; leonCAST
  doesn't fetch BPM from any external source.
- Crossfade length is configurable per station (`crossfade_seconds`),
  applied live via the segment-based streaming engine rather than baked
  into a pre-rendered file.

## Contributing

Issues and PRs welcome. This is a small self-hosted project, so keep changes
focused — if you're proposing something big (new storage backend, auth
scheme, etc.) open an issue first to talk it through.

## License

MIT — see [LICENSE](LICENSE).
