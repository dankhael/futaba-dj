"""Futaba DJ entry point. Wires intents, loads cogs, runs the client."""

from __future__ import annotations

import asyncio
import logging
import os

import discord
from discord.ext import commands
from dotenv import load_dotenv


def _build_intents() -> discord.Intents:
    intents = discord.Intents.default()
    # message_content is privileged — must also be enabled in the
    # Discord developer portal for `!`-prefix commands to fire.
    intents.message_content = True
    return intents


def _read_token() -> str:
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise RuntimeError(
            "DISCORD_TOKEN env var is unset; expected a Discord bot token string"
        )
    return token


async def _main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO)
    bot = commands.Bot(command_prefix="!", intents=_build_intents())

    @bot.event
    async def on_ready() -> None:
        print(f"Logged in as: {bot.user} (ID: {bot.user.id})")
        print("------")

    await bot.load_extension("cogs.music")
    await bot.start(_read_token())


if __name__ == "__main__":
    asyncio.run(_main())
