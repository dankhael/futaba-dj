# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Run

- Local: `pip install -r requirements.txt`, ensure `ffmpeg` is on PATH, set `DISCORD_TOKEN`, then `python bot.py`.
- Dev extras (tests + lint): `pip install -r requirements-dev.txt`.
- Docker: `docker build -t futaba_dj .` then `docker run -e DISCORD_TOKEN=... futaba_dj`. The image installs ffmpeg via apt.

## Lint / Format / Test

- Tests: `python -m pytest`
- Lint: `python -m ruff check .`
- Format: `python -m black .` (check-only: `python -m black --check .`)
- Tooling config lives in [pyproject.toml](pyproject.toml).

## Architecture

Discord music bot built on `discord.py[voice]` with `commands.Bot` (prefix `!`). Layered into:

- [bot.py](bot.py) — entry point: intents, env, loads cogs, runs the client.
- [cogs/music.py](cogs/music.py) — `Music` cog with all `!`-commands (`join`, `play`, `playnext`, `skip`, `pause`, `resume`, `stop`, `queue`, `nowplaying`, `remove`, `clear`, `shuffle`, `loop`, `volume`).
- [services/track_source.py](services/track_source.py) — `TrackSource`, the yt-dlp + FFmpeg adapter owned by this project. Two-phase: `probe()` for queue metadata, `build_audio()` for fresh stream URLs at playback time (YouTube signed URLs expire while a track sits in the queue).
- [services/guild_queue.py](services/guild_queue.py) — pure-data per-guild `GuildQueue` (loop modes, volume, FIFO + front-jump + remove + shuffle) and a `GuildQueueRegistry` keyed by `guild.id`. No discord/asyncio imports here so the queue is unit-testable in isolation.

Audio pipeline: `TrackSource._extract` calls yt-dlp's blocking `extract_info` inside `loop.run_in_executor` to avoid stalling the asyncio event loop, then streams the resolved direct URL through `discord.FFmpegPCMAudio` wrapped in `PCMVolumeTransformer`. Streaming only — files are not downloaded.

Auto-advance: `voice_client.play(after=...)` fires its callback on a discord.py audio thread (not the asyncio loop), so the cog hops back via `asyncio.run_coroutine_threadsafe`. Broken tracks are skipped in a loop (not recursion) so a streak of failures cannot blow the stack.

The FFmpeg `before_options` (`-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5`) exist because yt-dlp's resolved URLs frequently drop mid-stream; do not remove them when editing FFmpeg flags. `message_content` is a privileged intent and must be enabled in the Discord developer portal for `!`-prefix commands to fire.

## Code style

- Functions: 4-20 lines. Split if longer.
- Files: under 500 lines. Split by responsibility.
- One thing per function, one responsibility per module (SRP).
- Names: specific and unique. Avoid `data`, `handler`, `Manager`.
  Prefer names that return <5 grep hits in the codebase.
- Types: explicit. No `Any`, no bare `dict`/`Dict`, no untyped functions.
- No code duplication. Extract shared logic into a function/module.
- Early returns over nested ifs. Max 2 levels of indentation.
- Exception messages must include the offending value and expected shape.

## Comments

- Keep your own comments. Don't strip them on refactor — they carry
  intent and provenance.
- Write WHY, not WHAT. Skip `# increment counter` above `i += 1`.
- Docstrings on public functions: intent + one usage example.
- Reference issue numbers / commit SHAs when a line exists because
  of a specific bug or upstream constraint.

## Tests

- Run all tests: `python -m pytest`. A single test file:
  `python -m pytest tests/test_guild_queue.py -v`.
- Pure-data modules (e.g. `services/guild_queue.py`) keep discord/yt-dlp
  out of their imports so their tests don't need the voice stack.
- Every new function gets a test. Bug fixes get a regression test.
- Mock external I/O (Discord API, yt-dlp, FFmpeg, filesystem) with named
  fake classes (e.g. `FakeYoutubeDL`), not inline stubs.
- Tests must be F.I.R.S.T: fast, independent, repeatable,
  self-validating, timely.

## Dependencies

- Inject dependencies through constructor/parameter, not global/import.
- Wrap third-party libs behind a thin interface owned by this project.

## Structure

- Follow the framework's convention (`discord.py` cogs/commands layout
  if the bot grows beyond a single file).
- Prefer small focused modules over god files.
- Predictable paths: cogs/, services/, tests/, etc.

## Formatting

- Use the language default formatter (`black` for Python). Don't discuss
  style beyond that.
- Lint with `ruff` (config in `pyproject.toml`: pyflakes, pycodestyle,
  isort, bugbear, pyupgrade, simplify, ruff-specific).

## Logging

- Structured JSON when logging for debugging / observability.
- Plain text only for user-facing CLI output.
