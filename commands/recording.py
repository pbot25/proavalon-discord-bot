from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from database.recording_integrations import (
    get_recording_integration,
)
from utils.permissions import bot_admin_only
from youtube_oauth import authorization_url


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
            url = authorization_url(
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
            url = authorization_url(
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
