from __future__ import annotations

import hmac
import html
import os
from datetime import datetime, timezone

from aiohttp import ClientSession, web

from database.discord_roles import get_active_season_linked_users
from database.seasons import get_active_season
from database.recording_integrations import (
    delete_recording_integration,
    get_recording_integration,
    save_recording_integration,
)
from youtube_oauth import (
    callback_url,
    encrypt_refresh_token,
    required_environment,
    validate_oauth_state,
)


class RecorderBridge:
    """Small authenticated HTTP bridge from Railway pbot to recorders."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self.secret = os.getenv("RECORDER_BRIDGE_SECRET", "").strip()
        self.port = int(os.getenv("PORT", "8080"))
        self.runner: web.AppRunner | None = None

    async def start(self) -> None:
        youtube_configured = all(os.getenv(name, "").strip() for name in (
            "PUBLIC_BASE_URL",
            "GOOGLE_YOUTUBE_CLIENT_ID",
            "GOOGLE_YOUTUBE_CLIENT_SECRET",
            "YOUTUBE_OAUTH_STATE_SECRET",
            "YOUTUBE_TOKEN_ENCRYPTION_KEY",
        ))
        if not self.secret and not youtube_configured:
            print("Recorder web service disabled: bridge and YouTube are not configured.")
            return
        app = web.Application()
        app.router.add_get("/health", self.health)
        if self.secret:
            app.router.add_get("/recorder/lobbies", self.lobbies)
        if youtube_configured:
            app.router.add_get(
                "/recording/youtube/callback", self.youtube_callback
            )
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

    @staticmethod
    def _youtube_result_page(title: str, message: str, success: bool) -> web.Response:
        color = "#57F287" if success else "#ED4245"
        body = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{html.escape(title)}</title></head>
<body style="background:#111827;color:#f9fafb;font-family:system-ui;margin:0;padding:48px 20px">
<main style="max-width:620px;margin:auto;background:#1f2937;padding:32px;border-radius:16px;border-top:5px solid {color}">
<h1>{html.escape(title)}</h1><p>{html.escape(message)}</p>
<p>You may close this page and return to Discord.</p></main></body></html>"""
        return web.Response(text=body, content_type="text/html")

    async def youtube_callback(self, request: web.Request) -> web.Response:
        if request.query.get("error"):
            return self._youtube_result_page(
                "YouTube was not connected",
                "Google authorization was cancelled or denied.",
                False,
            )
        try:
            state = validate_oauth_state(request.query.get("state", ""))
            code = request.query.get("code", "").strip()
            if not code:
                raise ValueError("Google did not provide an authorization code.")

            async with ClientSession() as session:
                async with session.post(
                    "https://oauth2.googleapis.com/token",
                    data={
                        "code": code,
                        "client_id": required_environment("GOOGLE_YOUTUBE_CLIENT_ID"),
                        "client_secret": required_environment(
                            "GOOGLE_YOUTUBE_CLIENT_SECRET"
                        ),
                        "redirect_uri": callback_url(),
                        "grant_type": "authorization_code",
                    },
                ) as token_response:
                    token_data = await token_response.json()
                    if token_response.status != 200:
                        raise RuntimeError("Google rejected the authorization exchange.")

                access_token = str(token_data.get("access_token", ""))
                refresh_token = str(token_data.get("refresh_token", ""))
                if not access_token or not refresh_token:
                    raise RuntimeError(
                        "Google did not return the required long-lived authorization."
                    )

                async with session.get(
                    "https://www.googleapis.com/youtube/v3/channels",
                    params={"part": "snippet", "mine": "true"},
                    headers={"Authorization": f"Bearer {access_token}"},
                ) as channel_response:
                    channel_data = await channel_response.json()
                    if channel_response.status != 200:
                        raise RuntimeError("The connected YouTube channel could not be read.")

            channels = channel_data.get("items") or []
            if not channels:
                raise RuntimeError("That Google account has no YouTube channel.")
            channel = channels[0]
            channel_id = str(channel["id"])
            channel_name = str(channel.get("snippet", {}).get("title") or "YouTube")
            action = str(state.get("action", "enable"))
            guild_id = int(state["guild_id"])
            if action == "disable":
                current = await get_recording_integration(guild_id, "youtube")
                if not current:
                    return self._youtube_result_page(
                        "YouTube already disconnected",
                        "There is no connected YouTube channel for this Discord server.",
                        True,
                    )
                if str(current["account_id"]) != channel_id:
                    raise RuntimeError(
                        "That is not the YouTube channel currently connected to this server."
                    )
                await delete_recording_integration(guild_id, "youtube")
                async with ClientSession() as session:
                    async with session.post(
                        "https://oauth2.googleapis.com/revoke",
                        params={"token": refresh_token},
                    ):
                        pass
                return self._youtube_result_page(
                    "YouTube disconnected",
                    f"{channel_name} will no longer receive match recordings.",
                    True,
                )
            if action != "enable":
                raise ValueError("The YouTube connection action is invalid.")
            await save_recording_integration(
                guild_id,
                "youtube",
                channel_id,
                channel_name,
                f"https://www.youtube.com/channel/{channel_id}",
                encrypt_refresh_token(refresh_token),
            )
            return self._youtube_result_page(
                "YouTube connected",
                f"Pbot is now linked to {channel_name} for this Discord server.",
                True,
            )
        except (KeyError, RuntimeError, ValueError) as error:
            return self._youtube_result_page(
                "YouTube was not connected", str(error), False
            )

    async def lobbies(self, request: web.Request) -> web.Response:
        supplied = request.headers.get("Authorization", "")
        expected = f"Bearer {self.secret}"
        if not hmac.compare_digest(supplied, expected):
            raise web.HTTPUnauthorized()

        cog = self.bot.get_cog("LeaderboardCommands")
        tracker = getattr(cog, "rating_tracker", None)
        if tracker is None:
            raise web.HTTPServiceUnavailable(text="Live tracker is not ready.")
        links = {}
        recording_config = None
        voice_channels = []
        guild_id_text = request.query.get("guild_id", "").strip()
        if guild_id_text:
            try:
                guild_id = int(guild_id_text)
            except ValueError:
                raise web.HTTPBadRequest(text="guild_id must be a number.")
            season = await get_active_season(guild_id)
            youtube = await get_recording_integration(guild_id, "youtube")
            recording_config = {
                "enabled": bool(youtube and youtube["enabled"]),
                "youtube_enabled": bool(youtube and youtube["enabled"]),
                "minimum_linked_players_in_voice": 6,
                "postgame_seconds": 30,
            }
            guild = self.bot.get_guild(guild_id)
            if guild is not None:
                voice_channels = [
                    {
                        "channel_id": channel.id,
                        "name": channel.name,
                        "member_ids": [
                            member.id for member in channel.members
                            if not member.bot
                        ],
                    }
                    for channel in guild.voice_channels
                ]
            if season is not None:
                rows = await get_active_season_linked_users(
                    guild_id,
                    int(season["season_number"]),
                )
                links = {
                    str(row["proavalon_username"]).casefold(): int(
                        row["discord_user_id"]
                    )
                    for row in rows
                    if row["discord_user_id"] is not None
                }
        return web.json_response({
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "lobbies": tracker.get_recorder_lobby_snapshot(),
            "account_links": links,
            "recording_config": recording_config,
            "voice_channels": voice_channels,
        })
