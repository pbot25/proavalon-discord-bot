from __future__ import annotations

import hmac
import os
from datetime import datetime, timezone

from aiohttp import web


class RecorderBridge:
    """Small authenticated HTTP bridge from Railway pbot to recorders."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self.secret = os.getenv("RECORDER_BRIDGE_SECRET", "").strip()
        self.port = int(os.getenv("PORT", "8080"))
        self.runner: web.AppRunner | None = None

    async def start(self) -> None:
        if not self.secret:
            print("Recorder bridge disabled: RECORDER_BRIDGE_SECRET is not set.")
            return
        app = web.Application()
        app.router.add_get("/health", self.health)
        app.router.add_get("/recorder/lobbies", self.lobbies)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        await web.TCPSite(self.runner, "0.0.0.0", self.port).start()
        print(f"Recorder bridge listening on port {self.port}.")

    async def close(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None

    async def health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def lobbies(self, request: web.Request) -> web.Response:
        supplied = request.headers.get("Authorization", "")
        expected = f"Bearer {self.secret}"
        if not hmac.compare_digest(supplied, expected):
            raise web.HTTPUnauthorized()

        cog = self.bot.get_cog("LeaderboardCommands")
        tracker = getattr(cog, "rating_tracker", None)
        if tracker is None:
            raise web.HTTPServiceUnavailable(text="Live tracker is not ready.")
        return web.json_response({
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "lobbies": tracker.get_recorder_lobby_snapshot(),
        })
