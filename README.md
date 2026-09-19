# futaba_dj

A Discord music bot that streams audio from YouTube (and any other [yt-dlp](https://github.com/yt-dlp/yt-dlp)–supported source) into a voice channel, with a per-guild queue, loop modes, and the usual playback controls.

## Commands

All commands use the `!` prefix.

| Command | Aliases | Description |
| --- | --- | --- |
| `!join` | | Joins the voice channel of the caller. |
| `!play <url>` | | Streams a URL or search query. Enqueues if something is already playing. |
| `!playnext <url>` | | Inserts a track at the front of the queue (plays next). |
| `!skip` | `!next` | Skips the current track; the queue advances automatically. |
| `!pause` | | Pauses playback. |
| `!resume` | | Resumes a paused track. |
| `!stop` | | Clears the queue and disconnects from voice. |
| `!queue` | `!q` | Shows the current track and up to 20 upcoming entries. |
| `!nowplaying` | `!np` | Shows the currently-playing track. |
| `!remove <n\|last>` | `!rm` | Removes a queued track by 1-based position, or `last`. |
| `!clear` | | Clears pending tracks (current keeps playing). |
| `!shuffle` | | Shuffles the pending queue. |
| `!loop [off\|track\|queue\|toggle]` | | Sets loop mode. Bare `!loop` cycles modes. |
| `!volume <0..200>` | `!vol` | Sets playback volume in percent. Persists across queued tracks. |

## Requirements

- Python 3.9+
- [FFmpeg](https://ffmpeg.org/) on `PATH`
- A Discord bot token with the **Message Content** privileged intent enabled in the [Discord developer portal](https://discord.com/developers/applications)

## Running locally

```bash
pip install -r requirements.txt
export DISCORD_TOKEN=your-token-here   # Windows (cmd): set DISCORD_TOKEN=...
python bot.py
```

A `.env` file at the repo root with `DISCORD_TOKEN=...` is also picked up automatically (via `python-dotenv`).

## Running with Docker

```bash
cp .env.example .env   # set DISCORD_TOKEN
docker compose up -d --build
docker compose logs -f
```

The image is based on `python:3.12-slim` and installs FFmpeg via apt.

### YouTube cookies (required on VPS / datacenter IPs)

YouTube answers datacenter IPs with `Sign in to confirm you're not a bot`
for every yt-dlp player client. The workaround is a logged-in session
exported as a Netscape `cookies.txt` next to `docker-compose.yml`
(mounted at `/app/cookies.txt`; override with `YTDL_COOKIES_FILE`).

1. Use a **throwaway Google account** — YouTube may flag the account.
2. In an **incognito window**, log in to youtube.com, then export cookies
   with the "Get cookies.txt LOCALLY" extension (Chrome/Firefox).
3. Close the incognito window *without* logging out (logging out
   invalidates the exported session).
4. `scp cookies.txt vps:/root/futaba_dj/cookies.txt && docker compose restart`.

An empty or missing `cookies.txt` is ignored, so local runs on a
residential IP keep working without one.

### PO Token provider (fixes silent 403 skips)

Even with cookies, the stream URLs YouTube hands out on a datacenter IP
require a **GVS PO Token**; without one the CDN refuses roughly half of
them with `HTTP 403` — the bot says "Now playing" but FFmpeg dies before
the first frame. `docker-compose.yml` therefore runs a
[`bgutil-ytdlp-pot-provider`](https://github.com/Brainicism/bgutil-ytdlp-pot-provider)
sidecar and points yt-dlp at it via `YTDL_POT_PROVIDER_URL`. Nothing to
configure; just `docker compose up -d --build`.

Keep the image tag in `docker-compose.yml` and the plugin version in
`requirements.txt` identical — mismatched versions fail silently.

As a second line of defence the bot pre-flights every resolved URL with
a 1-byte ranged GET and re-extracts on 403 (up to 4 times); tracks it
still cannot stream are reported in the text channel instead of being
skipped silently.

To check the provider is wired up:

```bash
docker compose exec bot yt-dlp -v --cookies /app/cookies.txt \
  --extractor-args "youtube:fetch_pot=always" \
  --extractor-args "youtubepot-bgutilhttp:base_url=http://bgutil-provider:4416" \
  -g https://www.youtube.com/watch?v=dQw4w9WgXcQ 2>&1 | grep "\[pot"
# expect: "Retrieved a gvs PO Token for web_embedded client" — if only the
# "PO Token Providers: bgutil:http-2.0.0" line shows up, no token is being
# fetched (that is what fetch_pot=always fixes)
```

## Development

Install runtime + dev dependencies (pytest, black, ruff):

```bash
pip install -r requirements-dev.txt
```

Common commands:

```bash
python -m pytest          # run the test suite
python -m ruff check .    # lint
python -m black .         # format (use --check for CI)
```

Tooling config lives in [`pyproject.toml`](pyproject.toml).

## Project layout

```
bot.py                       # entry point: intents, env, loads cogs, runs the client
cogs/
  music.py                   # the Music cog with all !-commands
services/
  track_source.py            # yt-dlp + FFmpeg adapter (TrackSource, TrackInfo)
  guild_queue.py             # pure-data per-guild queue (GuildQueue, LoopMode, registry)
tests/
  test_guild_queue.py        # 24 unit tests for the queue
  test_track_source.py       # 9 unit tests for the yt-dlp adapter, with FakeYoutubeDL
```

`services/guild_queue.py` deliberately has no `discord` or `yt_dlp` imports so the queue is unit-testable without the voice stack.

## How it works

`TrackSource` is the project's owned wrapper around yt-dlp + FFmpeg. It runs in two phases so the queue stays responsive without paying the cost of resolving stream URLs that may expire:

1. **`probe(query)`** — runs yt-dlp's blocking `extract_info` inside `loop.run_in_executor` (so the asyncio event loop keeps servicing Discord heartbeats) and returns a `TrackInfo` for queue display.
2. **`build_audio(query)`** — re-resolves the stream URL right before playback and hands it to `discord.FFmpegPCMAudio` wrapped in a `PCMVolumeTransformer`. Streaming only — nothing is downloaded to disk.

The FFmpeg invocation includes `-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5` because yt-dlp's resolved URLs frequently drop mid-stream.

When a track ends, `voice_client.play(after=...)` fires its callback on a discord.py audio thread (not the asyncio loop), so the cog hops back to the loop with `asyncio.run_coroutine_threadsafe` and starts the next track. Failed tracks are skipped in a loop (not recursion) so a streak of broken entries can't blow the stack.
