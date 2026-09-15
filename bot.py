import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

from database.connection import initialize_database
from proavalon.profile_cache import ProfileCache
from proavalon.profile_scraper import (
    ProAvalonLoginError,
    ProAvalonProfileScraper,
)
from recorder_bridge import RecorderBridge


load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
PROAVALON_USERNAME = os.getenv("PROAVALON_USERNAME")
PROAVALON_PASSWORD = os.getenv("PROAVALON_PASSWORD")


if not DISCORD_TOKEN:
    raise RuntimeError(
        "DISCORD_TOKEN is missing from the .env file."
    )

if not PROAVALON_USERNAME or not PROAVALON_PASSWORD:
    raise RuntimeError(
        "PROAVALON_USERNAME or PROAVALON_PASSWORD "
        "is missing from the .env file."
    )


class ProAvalonBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()

        super().__init__(
            command_prefix="!",
            intents=intents,
        )

        self.proavalon_scraper = ProAvalonProfileScraper(
            minimum_delay_seconds=3.0,
        )

        self.profile_cache = ProfileCache(
            ttl_seconds=15 * 60,
        )
        self.recorder_bridge = RecorderBridge(self)

    async def setup_hook(self) -> None:
        print("Initializing database...")
        await initialize_database()

        print("Starting ProAvalon scraper...")
        await self.proavalon_scraper.start()

        print("Logging into ProAvalon...")

        try:
            await self.proavalon_scraper.login(
                username=PROAVALON_USERNAME,
                password=PROAVALON_PASSWORD,
            )
        except ProAvalonLoginError:
            await self.proavalon_scraper.close()
            raise

        print("ProAvalon login successful.")

        await self.load_extension("commands.admin")
        await self.load_extension("commands.account")
        await self.load_extension("commands.season")
        await self.load_extension("commands.stats")
        await self.load_extension("commands.leaderboard")
        await self.load_extension("commands.group")
        await self.load_extension("commands.insights")
        await self.load_extension("commands.roles")
        await self.load_extension("commands.recording")

        await self.recorder_bridge.start()

        synced_commands = await self.tree.sync()

        print(
            f"Synced {len(synced_commands)} Discord command(s)."
        )

    async def close(self) -> None:
        print("Closing recorder bridge...")
        await self.recorder_bridge.close()
        print("Closing ProAvalon scraper...")
        await self.proavalon_scraper.close()
        await super().close()


bot = ProAvalonBot()


@bot.event
async def on_ready() -> None:
    print(f"Logged into Discord as {bot.user}")


@bot.tree.command(
    name="ping",
    description="Checks whether the bot is online.",
)
async def ping(
    interaction: discord.Interaction,
) -> None:
    latency = round(bot.latency * 1000)

    await interaction.response.send_message(
        f"🏓 ProAvalon Stats is online. "
        f"Latency: {latency} ms."
    )


bot.run(DISCORD_TOKEN)
