from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from database.recording_settings import (
    get_recording_config,
    set_recording_enabled,
    set_recording_output_channel,
)
from utils.permissions import bot_admin_only


class RecordingCommands(
    commands.GroupCog,
    group_name="recording",
    group_description="Configure automatic match recording.",
):
    output_channel = app_commands.Group(
        name="output-channel",
        description="Configure where completed match recordings are posted.",
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="enable",
        description="Enables automatic recording for qualifying matches.",
    )
    @bot_admin_only()
    async def enable(self, interaction: discord.Interaction) -> None:
        await set_recording_enabled(interaction.guild_id, True)
        await interaction.response.send_message(
            "Automatic match recording is enabled for this server.",
            ephemeral=True,
        )

    @app_commands.command(
        name="disable",
        description="Disables automatic recording for future matches.",
    )
    @bot_admin_only()
    async def disable(self, interaction: discord.Interaction) -> None:
        await set_recording_enabled(interaction.guild_id, False)
        await interaction.response.send_message(
            (
                "Automatic match recording is disabled. "
                "An active recording is not interrupted."
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="status",
        description="Shows this server's automatic recording settings.",
    )
    @bot_admin_only()
    async def status(self, interaction: discord.Interaction) -> None:
        config = await get_recording_config(interaction.guild_id)
        enabled = bool(config and config["enabled"])
        output_channel_id = (
            str(config["output_channel_id"])
            if config and config["output_channel_id"]
            else None
        )
        destination = (
            f"<#{output_channel_id}>"
            if output_channel_id
            else "the normal match-history channel"
        )
        await interaction.response.send_message(
            "\n".join(
                (
                    f"**Automatic recording:** {'Enabled' if enabled else 'Disabled'}",
                    f"**Recording destination:** {destination}",
                    "**Recorder trigger:** Match start, with at least six linked match players in one VC",
                )
            ),
            ephemeral=True,
        )

    @output_channel.command(
        name="set",
        description="Sets a dedicated channel for completed match recordings.",
    )
    @app_commands.describe(channel="Channel that should receive completed recordings.")
    @bot_admin_only()
    async def set_output_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        await set_recording_output_channel(interaction.guild_id, channel.id)
        await interaction.response.send_message(
            f"Completed match recordings will be posted in {channel.mention}.",
            ephemeral=True,
        )

    @output_channel.command(
        name="clear",
        description="Uses the normal match-history channel for recordings.",
    )
    @bot_admin_only()
    async def clear_output_channel(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await set_recording_output_channel(interaction.guild_id, None)
        await interaction.response.send_message(
            (
                "The dedicated recording channel was cleared. Completed recordings "
                "will use the normal match-history channel."
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
