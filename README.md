# leonCAST

A lightweight, self-hosted alternative to AzuraCast. Multi-station internet
radio with BPM-aware crossfading, uploads, playlists, and scheduling —
without the bloat.

## Stack

- **Python** — station engine, API, scheduler
- **ffmpeg** — crossfaded segment rendering (`acrossfade` filter chains)
- **Icecast** — actual stream distribution
- **FastAPI** — REST API
- **SQLite** — stations / tracks / playlists / schedule
- **Vanilla JS dashboard** — Fluent Design–inspired control room UI

## How it works

Each station runs its own background thread (`StationEngine`) that:

1. Peeks the next few tracks off that station's `TrackQueue`
2. Builds an ffmpeg `filter_complex` chain of `acrossfade` filters across them
3. Streams the resulting segment straight to the station's Icecast mount
4. Advances the queue and repeats, so playback (and crossfading) never stops

`StationManager` owns all `StationEngine` instances so you can run as many
stations as your box can handle ffmpeg processes for.

The `Scheduler` runs a 30-second tick loop, checks each station's schedule
blocks (day-of-week + time range → playlist), and swaps in the right
playlist automatically — an ErsatzTV-lite for audio.

## Running it

```bash
pip install -r requirements.txt --break-system-packages

# 1. Start Icecast (see data/icecast.xml, generated per-station)
icecast2 -c data/icecast.xml

# 2. Start leonCAST
uvicorn api.main:app --host 0.0.0.0 --port 8080
```

Then open `http://localhost:8080` for the dashboard.

## API quick reference

| Method | Path | What |
|---|---|---|
| GET/POST | `/api/stations` | list / create stations |
| POST | `/api/stations/{id}/start` \| `/stop` | go live / take offline |
| GET | `/api/stations/{id}/now-playing` | current track |
| POST | `/api/stations/{id}/upload` | upload a track (multipart) |
| GET | `/api/stations/{id}/tracks` | list uploaded tracks |
| POST | `/api/playlists` | create a playlist |
| POST | `/api/playlists/{id}/activate?station_id=` | load a playlist live now |
| POST | `/api/schedule` | add a day/time → playlist block |

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
- Track duration/BPM/title/artist are read via `mutagen` on upload; BPM
  falls back to `null` if untagged (same as your existing generator scripts —
  you can still backfill from Beatport/Juno like before).
- Crossfade length is configurable per station (`crossfade_seconds` on
  `StationConfig`), same idea as the BPM-aware crossfades in your other
  radio scripts, just generalized to a live engine instead of a one-shot render.

## Contributing

Issues and PRs welcome. This is a small self-hosted project, so keep changes
focused — if you're proposing something big (new storage backend, auth
scheme, etc.) open an issue first to talk it through.

## License

MIT — see [LICENSE](LICENSE).
