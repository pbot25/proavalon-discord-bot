from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from database.recording_integrations import (
    get_pending_youtube_expiration_reminders,
    get_recording_integration,
    mark_youtube_expiration_reminder_sent,
)
from database.recording_audit import get_recording_audit
from utils.permissions import bot_admin_only
from youtube_oauth import connection_url


class RecordingCommands(
    commands.GroupCog,
    group_name="recording",
    group_description="Configure automatic match recording integrations.",
):
    youtube = app_commands.Group(
        name="youtube",
        description="Connect YouTube for automatic match recordings.",
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        testing_mode = os.getenv(
            "YOUTUBE_OAUTH_TESTING_MODE", "true"
        ).strip().casefold()
        if testing_mode not in {"0", "false", "no", "off"}:
            self.youtube_expiration_reminders.start()

    def cog_unload(self) -> None:
        self.youtube_expiration_reminders.cancel()

    @tasks.loop(minutes=30)
    async def youtube_expiration_reminders(self) -> None:
        try:
            rows = await get_pending_youtube_expiration_reminders()
        except Exception as error:
            print(f"Could not check YouTube expiration reminders: {error}")
            return

        now = datetime.now(timezone.utc)
        for row in rows:
            try:
                connected_at = datetime.fromisoformat(
                    str(row["updated_at"]).replace("Z", "+00:00")
                )
                if connected_at.tzinfo is None:
                    connected_at = connected_at.replace(tzinfo=timezone.utc)
                warning_at = connected_at + timedelta(days=6)
                if now < warning_at:
                    continue

                guild_id = int(row["guild_id"])
                user_id = int(row["authorized_user_id"])
                expires_at = connected_at + timedelta(days=7)
                expiry_timestamp = int(expires_at.timestamp())
                message = (
                    f"<@{user_id}> **YouTube connection reminder**\n"
                    f"PBot's testing-mode authorization for "
                    f"**{row['account_name']}** is expected to expire "
                    f"<t:{expiry_timestamp}:R>. Run `/recording youtube enable` "
                    "again today and complete the Google sign-in so automatic "
                    "match uploads continue."
                )

                delivered = False
                try:
                    user = self.bot.get_user(user_id) or await self.bot.fetch_user(
                        user_id
                    )
                    await user.send(message)
                    delivered = True
                except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                    output_channel_id = row["output_channel_id"]
                    if output_channel_id:
                        try:
                            channel = self.bot.get_channel(int(output_channel_id))
                            if channel is None:
                                channel = await self.bot.fetch_channel(
                                    int(output_channel_id)
                                )
                            await channel.send(message)
                            delivered = True
                        except (
                            discord.Forbidden,
                            discord.HTTPException,
                            discord.NotFound,
                            ValueError,
                        ):
                            pass

                if delivered:
                    await mark_youtube_expiration_reminder_sent(
                        guild_id,
                        str(row["updated_at"]),
                    )
                    print(
                        f"Sent YouTube expiration reminder to Discord user "
                        f"{user_id} for guild {guild_id}."
                    )
                else:
                    print(
                        f"Could not deliver YouTube expiration reminder to "
                        f"Discord user {user_id} for guild {guild_id}."
                    )
            except (KeyError, TypeError, ValueError) as error:
                print(f"Invalid YouTube expiration reminder row: {error}")

    @youtube_expiration_reminders.before_loop
    async def before_youtube_expiration_reminders(self) -> None:
        await self.bot.wait_until_ready()

    @youtube.command(
        name="enable",
        description="Connects a YouTube channel and enables YouTube recordings.",
    )
    @bot_admin_only()
    async def youtube_enable(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        try:
            url = connection_url(
                interaction.guild_id, interaction.user.id, "enable"
            )
        except (RuntimeError, ValueError):
            await interaction.response.send_message(
                "YouTube linking is not configured on Pbot yet.", ephemeral=True
            )
            return

        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Connect YouTube", url=url))
        await interaction.response.send_message(
            "Use this private button to choose the YouTube channel for match recordings. "
            "The link expires in 10 minutes.",
            view=view,
            ephemeral=True,
        )

    @youtube.command(
        name="disable",
        description="Disconnects YouTube and disables future YouTube recordings.",
    )
    @bot_admin_only()
    async def youtube_disable(self, interaction: discord.Interaction) -> None:
        current = await get_recording_integration(interaction.guild_id, "youtube")
        if not current:
            await interaction.response.send_message(
                "YouTube recording is already disabled.", ephemeral=True
            )
            return
        try:
            url = connection_url(
                interaction.guild_id, interaction.user.id, "disable"
            )
        except (RuntimeError, ValueError):
            await interaction.response.send_message(
                "YouTube linking is not configured on Pbot yet.", ephemeral=True
            )
            return

        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Verify and disconnect YouTube", url=url))
        await interaction.response.send_message(
            "Sign in to the currently connected YouTube channel to confirm disconnection. "
            "The link expires in 10 minutes.",
            view=view,
            ephemeral=True,
        )

    @app_commands.command(
        name="status",
        description="Shows connected recording services for this server.",
    )
    @bot_admin_only()
    async def status(self, interaction: discord.Interaction) -> None:
        youtube = await get_recording_integration(interaction.guild_id, "youtube")
        if youtube and youtube["enabled"]:
            youtube_status = (
                f"Enabled — [{youtube['account_name']}]({youtube['account_url']})"
            )
        else:
            youtube_status = "Disabled — no account connected"

        await interaction.response.send_message(
            "\n".join((
                f"**YouTube:** {youtube_status}",
                "**Twitch:** Not available yet",
            )),
            ephemeral=True,
        )

    @app_commands.command(
        name="audit",
        description="Shows the persistent recording timeline for a ProAvalon room.",
    )
    @app_commands.describe(room_id="The ProAvalon room number to inspect")
    @bot_admin_only()
    async def audit(self, interaction: discord.Interaction, room_id: int) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        rows = await get_recording_audit(interaction.guild_id, room_id, limit=20)
        if not rows:
            await interaction.response.send_message(
                f"No recording events were stored for room `{room_id}`.",
                ephemeral=True,
            )
            return
        lines = []
        length = len(f"**Recording audit for room {room_id}**\n")
        for row in rows:
            timestamp = str(row["created_at"]).replace("T", " ")[:19]
            recorder = f" • {row['recorder_id']}" if row["recorder_id"] else ""
            raw_detail = str(row["detail"] or "")
            detail = f" — {raw_detail[:180]}" if raw_detail else ""
            line = f"`{timestamp} UTC` **{row['stage']}**{recorder}{detail}"
            if length + len(line) + 1 > 1900:
                lines.append("…older events omitted")
                break
            lines.append(line)
            length += len(line) + 1
        await interaction.response.send_message(
            f"**Recording audit for room {room_id}**\n" + "\n".join(lines),
            ephemeral=True,
        )

    @app_commands.command(
        name="halt",
        description="Immediately discards an active abandoned/cancelled recording.",
    )
    @app_commands.describe(
        room_id=(
            "ProAvalon room to discard. Leave blank when only one room is active."
        )
    )
    @bot_admin_only()
    async def halt(
        self,
        interaction: discord.Interaction,
        room_id: int | None = None,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        try:
            cancellation = await self.bot.recorder_bridge.request_room_cancellation(
                interaction.guild_id,
                interaction.user.id,
                room_id,
            )
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return

        recorder = str(cancellation.get("recorder_id") or "the recorder")
        cancelled_room = int(cancellation["room_id"])
        await interaction.response.send_message(
            (
                f"Room `{cancelled_room}` was marked for immediate discard by "
                f"**{recorder}**. Browser video, Craig audio, and temporary "
                "media will be deleted, and the worker will return to lobby detection."
            ),
            ephemeral=True,
        )

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.CheckFailure):
            message = "You do not have bot administrator permission."
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
            return
        raise error


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RecordingCommands(bot))
