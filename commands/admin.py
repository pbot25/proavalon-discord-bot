from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from database.admins import (
    add_bot_admin,
    get_all_bot_admin_ids,
    remove_bot_admin,
)
from database.command_settings import (
    get_commands_channel_id,
    get_feedback_channel_id,
    set_commands_channel,
    set_feedback_channel,
)
from utils.permissions import (
    bot_admin_only,
    user_is_bot_admin,
)
from utils.discord_users import resolve_discord_target


ADMIN_SYMBOL = "🔒"


async def command_channel_interaction_check(
    interaction: discord.Interaction,
) -> bool:
    if interaction.guild_id is None:
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "Bot commands can only be used inside a server.",
                ephemeral=True,
            )
        return False

    if await user_is_bot_admin(interaction):
        return True

    channel_id = await get_commands_channel_id(
        interaction.guild_id
    )

    if channel_id is None:
        if not interaction.response.is_done():
            await interaction.response.send_message(
                (
                    "A commands channel has not been configured yet. "
                    "Ask a bot administrator to use "
                    "`/set commands channel`."
                ),
                ephemeral=True,
            )
        return False

    if interaction.channel_id != channel_id:
        if not interaction.response.is_done():
            await interaction.response.send_message(
                (
                    "Non-admin bot commands can only be used in "
                    f"<#{channel_id}>."
                ),
                ephemeral=True,
            )
        return False

    return True


def _is_admin_only(command) -> bool:
    current = command
    while current is not None:
        for check in getattr(current, "checks", []):
            if getattr(check, "__bot_admin_only__", False):
                return True
        current = getattr(current, "parent", None)
    return False


def _walk(command):
    if isinstance(command, app_commands.Group):
        for child in command.commands:
            yield from _walk(child)
    else:
        yield command


def _category(command) -> str:
    root = command.root_parent
    if root is not None:
        return root.name.replace("-", " ").title()
    name = command.name
    if name in {"leaderboard", "role-leaderboard", "records"}:
        return "Leaderboard"
    if name in {"recent", "match"}:
        return "Match History"
    if name in {"streaks", "role-stats", "alignment-stats", "activity", "player-history", "rivals", "teammates", "whois"}:
        return "Stats"
    if name == "bot-status":
        return "Admin"
    return "General"


def _usage(command) -> str:
    parts = [f"/{command.qualified_name}"]
    for parameter in command.parameters:
        name = parameter.display_name
        parts.append(
            f"<{name}>"
            if parameter.required
            else f"[{name}]"
        )
    return " ".join(parts)


def build_command_pages(bot: commands.Bot):
    categories = {}

    for root in bot.tree.get_commands():
        for command in _walk(root):
            categories.setdefault(
                _category(command),
                [],
            ).append(command)

    order = [
        "General",
        "Account",
        "Stats",
        "Match History",
        "Leaderboard",
        "Season",
        "Group",
        "Roles",
        "Admin",
        "Set",
    ]

    names = [name for name in order if name in categories]
    names += sorted(
        name for name in categories if name not in order
    )

    return [
        (
            name,
            sorted(
                categories[name],
                key=lambda command: command.qualified_name,
            ),
        )
        for name in names
    ]


class CommandHelpView(discord.ui.View):
    def __init__(self, bot: commands.Bot, user_id: int) -> None:
        super().__init__(timeout=120)
        self.bot = bot
        self.user_id = user_id
        self.pages = build_command_pages(bot)
        self.page = 0
        self._sync()

    def _sync(self) -> None:
        self.previous.disabled = self.page == 0
        self.next.disabled = self.page >= len(self.pages) - 1

    def embed(self) -> discord.Embed:
        category, items = self.pages[self.page]
        lines = []

        for command in items:
            lock = f"{ADMIN_SYMBOL} " if _is_admin_only(command) else ""
            lines.append(
                f"{lock}**`{_usage(command)}`**\n"
                f"{command.description or 'No description.'}"
            )

        embed = discord.Embed(
            title=f"Commands — {category}",
            description="\n\n".join(lines),
            color=discord.Color.blurple(),
        )
        embed.set_footer(
            text=(
                f"Page {self.page + 1}/{len(self.pages)} • "
                f"{ADMIN_SYMBOL} = admin only"
            )
        )
        return embed

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "Only the person who opened this list can use these buttons.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(
        label="Previous",
        emoji="◀️",
        style=discord.ButtonStyle.secondary,
    )
    async def previous(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        self.page -= 1
        self._sync()
        await interaction.response.edit_message(
            embed=self.embed(),
            view=self,
        )

    @discord.ui.button(
        label="Next",
        emoji="▶️",
        style=discord.ButtonStyle.secondary,
    )
    async def next(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        self.page += 1
        self._sync()
        await interaction.response.edit_message(
            embed=self.embed(),
            view=self,
        )


class AdminCommands(
    commands.GroupCog,
    group_name="admin",
    group_description="Manage ProAvalon Stats bot administrators.",
):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="add",
        description="Gives a Discord user bot administrator access.",
    )
    @app_commands.describe(
        user="Discord @ (if they are in the server).",
        user_id="Discord user ID (also works if they left).",
    )
    @bot_admin_only()
    async def add_admin(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
        user_id: str | None = None,
    ) -> None:
        try:
            target_id, target_mention = resolve_discord_target(user, user_id)
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return

        await add_bot_admin(
            guild_id=interaction.guild_id,
            discord_user_id=target_id,
            added_by_discord_user_id=interaction.user.id,
        )
        await interaction.response.send_message(
            f"{target_mention} (`{target_id}`) is now a bot administrator.",
            ephemeral=True,
        )

    @app_commands.command(
        name="remove",
        description="Removes a user's bot administrator access.",
    )
    @app_commands.describe(
        user="Discord @ (if they are in the server).",
        user_id="Discord user ID (works even if they left).",
    )
    @bot_admin_only()
    async def remove_admin(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
        user_id: str | None = None,
    ) -> None:
        try:
            target_id, target_mention = resolve_discord_target(user, user_id)
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return

        removed = await remove_bot_admin(interaction.guild_id, target_id)
        if not removed:
            await interaction.response.send_message(
                f"{target_mention} (`{target_id}`) is not a configured bot administrator.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"Removed bot administrator access from {target_mention} (`{target_id}`).",
            ephemeral=True,
        )

    @app_commands.command(
        name="list",
        description="Lists server admins and manually assigned bot admins.",
    )
    async def list_admins(
        self,
        interaction: discord.Interaction,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        manual_admin_ids = await get_all_bot_admin_ids(
            interaction.guild_id
        )

        server_admins = [
            member
            for member in interaction.guild.members
            if (
                not member.bot
                and member.guild_permissions.administrator
            )
        ]
        server_admins.sort(
            key=lambda member: member.display_name.casefold()
        )

        manual_only_ids = [
            admin_id
            for admin_id in manual_admin_ids
            if all(
                member.id != admin_id
                for member in server_admins
            )
        ]

        embed = discord.Embed(
            title="Bot Administrators",
            description=(
                "Server administrators automatically have bot-admin access. "
                "Manually assigned bot admins are granted access through `/admin add`."
            ),
            color=discord.Color.blurple(),
        )

        if server_admins:
            server_lines = [
                f"{member.mention} (`{member.id}`)"
                for member in server_admins
            ]
            embed.add_field(
                name=f"Server Administrators ({len(server_admins)})",
                value="\n".join(server_lines)[:1024],
                inline=False,
            )
        else:
            embed.add_field(
                name="Server Administrators (0)",
                value="None found.",
                inline=False,
            )

        if manual_only_ids:
            manual_lines = [
                f"<@{admin_id}> (`{admin_id}`)"
                for admin_id in manual_only_ids
            ]
            embed.add_field(
                name=f"Manually Assigned Bot Admins ({len(manual_only_ids)})",
                value="\n".join(manual_lines)[:1024],
                inline=False,
            )
        else:
            embed.add_field(
                name="Manually Assigned Bot Admins (0)",
                value="None configured.",
                inline=False,
            )

        overlap_ids = [
            admin_id
            for admin_id in manual_admin_ids
            if any(
                member.id == admin_id
                for member in server_admins
            )
        ]

        if overlap_ids:
            overlap_lines = [
                f"<@{admin_id}> (`{admin_id}`)"
                for admin_id in overlap_ids
            ]
            embed.add_field(
                name=f"Both Server + Manual ({len(overlap_ids)})",
                value="\n".join(overlap_lines)[:1024],
                inline=False,
            )

        await interaction.response.send_message(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.CheckFailure):
            message = "You do not have bot administrator permission."
            if interaction.response.is_done():
                await interaction.followup.send(
                    message,
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    message,
                    ephemeral=True,
                )
            return
        raise error


class SetCommands(
    commands.GroupCog,
    group_name="set",
    group_description="Configure server-specific bot settings.",
):
    commands_group = app_commands.Group(
        name="commands",
        description="Configure command usage.",
    )
    feedback_group = app_commands.Group(
        name="feedback",
        description="Configure feedback submissions.",
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands_group.command(
        name="channel",
        description="Sets where non-admins may use bot commands.",
    )
    @app_commands.describe(
        channel="The designated bot commands channel.",
    )
    @bot_admin_only()
    async def commands_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        await set_commands_channel(
            interaction.guild_id,
            channel.id,
        )
        await interaction.response.send_message(
            (
                "Non-admin commands are now restricted to "
                f"{channel.mention}."
            ),
            ephemeral=True,
        )

    @feedback_group.command(
        name="channel",
        description="Sets the channel where submitted feedback is posted.",
    )
    @app_commands.describe(
        channel="The channel that should receive feedback submissions.",
    )
    @bot_admin_only()
    async def feedback_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        await set_feedback_channel(
            interaction.guild_id,
            channel.id,
        )
        await interaction.response.send_message(
            f"Feedback submissions will now be posted in {channel.mention}.",
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
                await interaction.followup.send(
                    message,
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    message,
                    ephemeral=True,
                )
            return
        raise error


class UtilityCommands(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="commands",
        description="Shows all bot commands by category.",
    )
    async def command_list(
        self,
        interaction: discord.Interaction,
    ) -> None:
        view = CommandHelpView(self.bot, interaction.user.id)
        await interaction.response.send_message(
            embed=view.embed(),
            view=view,
        )

    @app_commands.command(
        name="feedback",
        description="Submits feedback to this server's configured feedback channel.",
    )
    @app_commands.describe(
        subject="Short subject used as the feedback embed title.",
        message="Your feedback, suggestion, bug report, or other comment.",
        anonymous="Hide your Discord identity from the submitted feedback. Defaults to false.",
    )
    async def feedback(
        self,
        interaction: discord.Interaction,
        subject: app_commands.Range[str, 1, 256],
        message: app_commands.Range[str, 1, 1500],
        anonymous: bool = False,
    ) -> None:
        channel_id = await get_feedback_channel_id(
            interaction.guild_id
        )

        if channel_id is None:
            await interaction.response.send_message(
                "This server has not configured a feedback channel yet.",
                ephemeral=True,
            )
            return

        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                channel = None

        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                (
                    "The configured feedback channel is unavailable. "
                    "Ask a bot administrator to set it again with "
                    "`/set feedback channel`."
                ),
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title=str(subject),
            description=str(message),
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="Submitted by",
            value=(
                "**Anonymous**"
                if anonymous
                else (
                    f"{interaction.user.mention}\n"
                    f"`{interaction.user}` • `{interaction.user.id}`"
                )
            ),
            inline=False,
        )
        embed.set_footer(
            text=f"Server: {interaction.guild.name} • {interaction.guild_id}"
        )

        try:
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            await interaction.response.send_message(
                (
                    "I could not post to the configured feedback channel. "
                    "Please ask an administrator to check my channel permissions."
                ),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "Feedback submitted successfully.",
            ephemeral=True,
        )



async def setup(bot: commands.Bot) -> None:
    bot.tree.interaction_check = command_channel_interaction_check
    await bot.add_cog(AdminCommands(bot))
    await bot.add_cog(SetCommands(bot))
    await bot.add_cog(UtilityCommands(bot))
