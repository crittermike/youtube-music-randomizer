YOUTUBE MUSIC RANDOMIZER

Generates a random-ish playlist by sampling recordings from MusicBrainz,
matching them to YouTube Music, and filtering by view count.

QUICK START (MACOS)

1. Double-click run.command.
2. Your browser opens the app at http://127.0.0.1:8787.
3. Generate a list and open any song directly in YouTube Music.
4. Click Open playlist in YouTube.
5. In regular YouTube, choose Save -> Create new playlist. Music tracks
   normally make that playlist available in YouTube Music too.

TERMINAL

Requires Python 3.9 or newer.

  python3 -m venv .venv
  .venv/bin/python -m pip install -r requirements.txt
  .venv/bin/python random_music.py serve

HOW IT WORKS

The generator samples MusicBrainz metadata across the selected year range,
matches each recording on YouTube Music, applies the requested minimum view
count, and keeps one song per artist. It is not a uniform sample of either
catalog.

HOSTING

The app includes a local Python backend and does not run on GitHub Pages as-is.
GitHub Pages can host only static files. A hosted version needs a separate
backend.

FLY.IO DEPLOYMENT

A Dockerfile and fly.toml are included. The container listens on 0.0.0.0:8080
(via the HOST and PORT env vars) and exposes a health check at /healthz. No
secrets are required; ytmusicapi is used unauthenticated.

  fly launch --no-deploy   # first time only, to create the app
  fly deploy               # build and release

Config: primary region iad, internal port 8080, HTTPS enforced,
shared-cpu-1x with 256 MB, machines auto-stop/start with zero kept running.
After deploy, the app is available at https://<app-name>.fly.dev.

