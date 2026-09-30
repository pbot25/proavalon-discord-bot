from __future__ import annotations

import hmac
import html
import os
import asyncio
import time
from datetime import datetime, timezone

from aiohttp import ClientSession, web

from database.discord_roles import get_active_season_linked_users
from database.seasons import get_active_season, season_display_name
from database.recording_integrations import (
    delete_recording_integration,
    get_recording_integration,
    save_recording_integration,
)
from database.recording_audit import append_recording_audit
from database.matches import (
    get_match_for_recording,
    save_youtube_recording,
    set_recording_status,
)
from youtube_oauth import (
    authorization_url_from_state,
    callback_url,
    decrypt_refresh_token,
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
        self.room_claims: dict[tuple[int, int], dict[str, object]] = {}
        self.claim_lock = asyncio.Lock()

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
            app.router.add_post("/recorder/claims", self.claim_room)
            app.router.add_post("/recorder/claims/heartbeat", self.heartbeat_claim)
            app.router.add_delete("/recorder/claims", self.release_claim)
            app.router.add_post("/recorder/audit", self.recording_audit)
            app.router.add_post(
                "/recorder/youtube/uploads/start", self.youtube_upload_start
            )
            app.router.add_post(
                "/recorder/youtube/uploads/complete", self.youtube_upload_complete
            )
        if youtube_configured:
            app.router.add_get(
                "/recording/youtube/connect", self.youtube_connect
            )
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

    async def youtube_connect(self, request: web.Request) -> web.Response:
        state = request.query.get("state", "")
        try:
            validate_oauth_state(state)
        except ValueError as error:
            return self._youtube_result_page(
                "YouTube was not connected", str(error), False
            )
        raise web.HTTPFound(authorization_url_from_state(state))

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

    def _authorize_recorder(self, request: web.Request) -> None:
        supplied = request.headers.get("Authorization", "")
        expected = f"Bearer {self.secret}"
        if not self.secret or not hmac.compare_digest(supplied, expected):
            raise web.HTTPUnauthorized()

    @staticmethod
    def _claim_identity(body: dict) -> tuple[int, int, str]:
        try:
            guild_id = int(body["guild_id"])
            room_id = int(body["room_id"])
            recorder_id = str(body["recorder_id"]).strip()
        except (KeyError, TypeError, ValueError):
            raise web.HTTPBadRequest(
                text="guild_id, room_id, and recorder_id are required."
            )
        if guild_id <= 0 or room_id <= 0 or not recorder_id:
            raise web.HTTPBadRequest(text="The recorder claim identity is invalid.")
        return guild_id, room_id, recorder_id

    def _discard_expired_claims(self) -> None:
        now = time.monotonic()
        expired = [
            key for key, claim in self.room_claims.items()
            if float(claim["expires_at"]) <= now
        ]
        for key in expired:
            self.room_claims.pop(key, None)

    async def claim_room(self, request: web.Request) -> web.Response:
        self._authorize_recorder(request)
        guild_id, room_id, recorder_id = self._claim_identity(await request.json())
        async with self.claim_lock:
            self._discard_expired_claims()
            key = (guild_id, room_id)
            current = self.room_claims.get(key)
            if current is not None and current["recorder_id"] != recorder_id:
                return web.json_response({
                    "claimed": False,
                    "recorder_id": current["recorder_id"],
                })
            self.room_claims[key] = {
                "recorder_id": recorder_id,
                "expires_at": time.monotonic() + 15,
            }
        return web.json_response({"claimed": True, "recorder_id": recorder_id})

    async def heartbeat_claim(self, request: web.Request) -> web.Response:
        self._authorize_recorder(request)
        guild_id, room_id, recorder_id = self._claim_identity(await request.json())
        async with self.claim_lock:
            self._discard_expired_claims()
            key = (guild_id, room_id)
            current = self.room_claims.get(key)
            if current is None or current["recorder_id"] != recorder_id:
                return web.json_response({"claimed": False})
            current["expires_at"] = time.monotonic() + 15
        return web.json_response({"claimed": True})

    async def release_claim(self, request: web.Request) -> web.Response:
        self._authorize_recorder(request)
        guild_id, room_id, recorder_id = self._claim_identity(await request.json())
        async with self.claim_lock:
            key = (guild_id, room_id)
            current = self.room_claims.get(key)
            if current is not None and current["recorder_id"] == recorder_id:
                self.room_claims.pop(key, None)
        return web.json_response({"ok": True})

    async def recording_audit(self, request: web.Request) -> web.Response:
        self._authorize_recorder(request)
        body = await request.json()
        try:
            guild_id = int(body["guild_id"])
            room_id = int(body["room_id"])
            stage = str(body["stage"]).strip()
        except (KeyError, TypeError, ValueError):
            raise web.HTTPBadRequest(
                text="guild_id, room_id, and stage are required."
            )
        if guild_id <= 0 or room_id <= 0 or not stage or len(stage) > 64:
            raise web.HTTPBadRequest(text="The recording audit event is invalid.")
        recorder_id = str(body.get("recorder_id") or "").strip() or None
        detail = str(body.get("detail") or "").strip() or None
        await append_recording_audit(
            guild_id,
            room_id,
            stage,
            recorder_id=recorder_id[:128] if recorder_id else None,
            detail=detail[:1000] if detail else None,
        )
        return web.json_response({"ok": True})

    async def _youtube_access_token(self, integration) -> str:
        refresh_token = decrypt_refresh_token(
            str(integration["encrypted_refresh_token"])
        )
        async with ClientSession() as session:
            async with session.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": required_environment("GOOGLE_YOUTUBE_CLIENT_ID"),
                    "client_secret": required_environment(
                        "GOOGLE_YOUTUBE_CLIENT_SECRET"
                    ),
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
            ) as response:
                data = await response.json()
                if response.status != 200 or not data.get("access_token"):
                    raise web.HTTPBadGateway(
                        text="YouTube authorization could not be refreshed."
                    )
                return str(data["access_token"])

    async def youtube_upload_start(self, request: web.Request) -> web.Response:
        self._authorize_recorder(request)
        body = await request.json()
        try:
            guild_id = int(body["guild_id"])
            room_id = int(body["room_id"])
            file_size = int(body["file_size"])
        except (KeyError, TypeError, ValueError):
            raise web.HTTPBadRequest(text="guild_id, room_id, and file_size are required.")

        match = await get_match_for_recording(guild_id, room_id)
        if match is None:
            raise web.HTTPConflict(text="The matching Pbot match is not archived yet.")
        if match["youtube_url"]:
            return web.json_response({
                "already_complete": True,
                "youtube_url": str(match["youtube_url"]),
            })
        integration = await get_recording_integration(guild_id, "youtube")
        if integration is None or not integration["enabled"]:
            raise web.HTTPConflict(text="YouTube is not enabled for this server.")

        access_token = await self._youtube_access_token(integration)
        privacy = os.getenv("YOUTUBE_PRIVACY_STATUS", "unlisted").strip().lower()
        if privacy not in {"private", "unlisted", "public"}:
            privacy = "unlisted"
        season_name = str(match["season_display_name"] or "ProAvalon").strip()
        title = f"{season_name} Match #{match['season_match_id']}"
        metadata = {
            "snippet": {
                "title": title,
                "description": (
                    f"Automatically recorded ProAvalon match #{match['season_match_id']} "
                    f"from room {room_id}."
                ),
            },
            "status": {"privacyStatus": privacy},
        }
        async with ClientSession() as session:
            async with session.post(
                "https://www.googleapis.com/upload/youtube/v3/videos",
                params={"uploadType": "resumable", "part": "snippet,status"},
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json; charset=UTF-8",
                    "X-Upload-Content-Length": str(file_size),
                    "X-Upload-Content-Type": "video/mp4",
                },
                json=metadata,
            ) as response:
                upload_url = response.headers.get("Location")
                if response.status not in {200, 201} or not upload_url:
                    raise web.HTTPBadGateway(
                        text="YouTube did not create an upload session."
                    )
        await set_recording_status(int(match["id"]), "uploading")
        await append_recording_audit(
            guild_id, room_id, "youtube_upload_started",
            detail=f"Preparing {file_size} byte upload.",
        )
        return web.json_response({"upload_url": upload_url})

    async def youtube_upload_complete(self, request: web.Request) -> web.Response:
        self._authorize_recorder(request)
        body = await request.json()
        try:
            guild_id = int(body["guild_id"])
            room_id = int(body["room_id"])
            video_id = str(body["video_id"]).strip()
        except (KeyError, TypeError, ValueError):
            raise web.HTTPBadRequest(text="guild_id, room_id, and video_id are required.")
        if not video_id:
            raise web.HTTPBadRequest(text="video_id is required.")
        match = await get_match_for_recording(guild_id, room_id)
        if match is None:
            raise web.HTTPNotFound(text="The matching Pbot match was not found.")

        youtube_url = f"https://youtu.be/{video_id}"
        await save_youtube_recording(int(match["id"]), video_id, youtube_url)
        await append_recording_audit(
            guild_id, room_id, "youtube_upload_completed", detail=youtube_url
        )
        channel_id = match["posted_channel_id"]
        message_id = match["posted_message_id"]
        if channel_id and message_id:
            try:
                channel = self.bot.get_channel(int(channel_id))
                if channel is None:
                    channel = await self.bot.fetch_channel(int(channel_id))
                message = await channel.fetch_message(int(message_id))
                if message.embeds:
                    embed = message.embeds[0].copy()
                    recording_index = next(
                        (
                            index for index, field in enumerate(embed.fields)
                            if field.name == "Recording"
                        ),
                        None,
                    )
                    value = f"[Watch the full match on YouTube]({youtube_url})"
                    if recording_index is None:
                        embed.add_field(name="Recording", value=value, inline=False)
                    else:
                        embed.set_field_at(
                            recording_index,
                            name="Recording",
                            value=value,
                            inline=False,
                        )
                    # Fetched embeds expose an uploaded attachment as its CDN
                    # URL. Reusing that URL while retaining the attachment
                    # makes Discord render the same voting chart twice: once
                    # as a standalone attachment and once inside the embed.
                    # Restore the attachment:// reference during the edit so
                    # the existing chart remains embedded exactly once.
                    chart_attachment = next(
                        (
                            attachment
                            for attachment in message.attachments
                            if attachment.filename == "voting-history.png"
                        ),
                        None,
                    )
                    if chart_attachment is not None:
                        embed.set_image(
                            url=f"attachment://{chart_attachment.filename}"
                        )
                    await message.edit(
                        embed=embed,
                        attachments=message.attachments,
                    )
                    await append_recording_audit(
                        guild_id, room_id, "match_history_linked", detail=youtube_url
                    )
            except Exception as error:
                print(f"YouTube upload saved, but match embed update failed: {error}")
                await append_recording_audit(
                    guild_id,
                    room_id,
                    "match_history_link_failed",
                    detail=str(error),
                )
        return web.json_response({"ok": True, "youtube_url": youtube_url})

    async def lobbies(self, request: web.Request) -> web.Response:
        self._authorize_recorder(request)

        cog = self.bot.get_cog("LeaderboardCommands")
        tracker = getattr(cog, "rating_tracker", None)
        if tracker is None:
            raise web.HTTPServiceUnavailable(text="Live tracker is not ready.")
        links = {}
        recording_config = None
        voice_channels = []
        season = None
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
        async with self.claim_lock:
            self._discard_expired_claims()
            claims = [
                {
                    "guild_id": claim_guild_id,
                    "room_id": room_id,
                    "recorder_id": claim["recorder_id"],
                }
                for (claim_guild_id, room_id), claim in self.room_claims.items()
                if not guild_id_text or claim_guild_id == int(guild_id_text)
            ]
        return web.json_response({
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "active_season": (
                {
                    "season_number": int(season["season_number"]),
                    "required_prefix": str(season["required_prefix"]),
                    "display_name": season_display_name(season),
                }
                if season is not None
                else None
            ),
            "lobbies": tracker.get_recorder_lobby_snapshot(),
            "account_links": links,
            "recording_config": recording_config,
            "voice_channels": voice_channels,
            "room_claims": claims,
        })
