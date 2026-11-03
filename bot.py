import asyncio
import os
import time

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
        self.gateway_watchdog_task: asyncio.Task | None = None
        self.gateway_disconnected_at: float | None = None
        self.gateway_was_ready = False
        self.restart_requested = False
        self.resources_closed = False

    async def gateway_watchdog(self) -> None:
        """Recycle Pbot when Discord remains disconnected for three minutes."""
        try:
            await self.wait_until_ready()
            self.gateway_was_ready = True
            while not self.is_closed():
                await asyncio.sleep(30)
                if self.is_ready():
                    self.gateway_disconnected_at = None
                    continue

                now = time.monotonic()
                if self.gateway_disconnected_at is None:
                    self.gateway_disconnected_at = now
                    continue

                disconnected_for = now - self.gateway_disconnected_at
                if disconnected_for < 180:
                    continue

                print(
                    "Discord gateway remained disconnected for "
                    f"{int(disconnected_for)} seconds; recycling Pbot.",
                    flush=True,
                )
                self.restart_requested = True
                await self.close()
                return
        except asyncio.CancelledError:
            raise
        except RuntimeError:
            # wait_until_ready raises if the bot closes during startup.
            return

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
        await self.load_extension("commands.insights")
        await self.load_extension("commands.roles")
        await self.load_extension("commands.recording")

        await self.recorder_bridge.start()

        synced_commands = await self.tree.sync()

        print(
            f"Synced {len(synced_commands)} Discord command(s)."
        )
        self.gateway_watchdog_task = asyncio.create_task(
            self.gateway_watchdog(),
            name="discord-gateway-watchdog",
        )

    async def close(self) -> None:
        if self.resources_closed:
            await super().close()
            return
        self.resources_closed = True
        watchdog = self.gateway_watchdog_task
        if watchdog is not None and watchdog is not asyncio.current_task():
            watchdog.cancel()
        print("Closing recorder bridge...")
        await self.recorder_bridge.close()
        print("Closing ProAvalon scraper...")
        await self.proavalon_scraper.close()
        await super().close()


def create_bot() -> ProAvalonBot:
    bot = ProAvalonBot()

    @bot.event
    async def on_ready() -> None:
        bot.gateway_was_ready = True
        bot.gateway_disconnected_at = None
        print(f"Logged into Discord as {bot.user}", flush=True)

    @bot.event
    async def on_disconnect() -> None:
        if bot.gateway_was_ready and bot.gateway_disconnected_at is None:
            bot.gateway_disconnected_at = time.monotonic()
        print("Discord gateway disconnected; waiting for reconnection.", flush=True)

    @bot.event
    async def on_resumed() -> None:
        bot.gateway_disconnected_at = None
        print("Discord gateway session resumed.", flush=True)

    @bot.tree.command(
        name="ping",
        description="Checks whether the bot is online.",
    )
    async def ping(
        interaction: discord.Interaction,
    ) -> None:
        latency = round(bot.latency * 1000)
        await interaction.response.send_message(
            f"🏓 ProAvalon Stats is online. Latency: {latency} ms."
        )

    return bot


async def run_bot_forever() -> None:
    """Retry a Cloudflare-blocked Discord login without a crash loop."""
    retry_delay = 10 * 60
    while True:
        bot = create_bot()
        try:
            async with bot:
                await bot.start(DISCORD_TOKEN)
            print(
                "Discord client stopped; starting a fresh Pbot session in 10 seconds.",
                flush=True,
            )
            await asyncio.sleep(10)
            retry_delay = 10 * 60
            continue
        except discord.HTTPException as error:
            if getattr(error, "status", None) != 429:
                raise

            print(
                "Discord login is temporarily rate-limited. "
                f"Keeping the service alive and retrying in "
                f"{retry_delay // 60} minute(s).",
                flush=True,
            )
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60 * 60)


asyncio.run(run_bot_forever())
