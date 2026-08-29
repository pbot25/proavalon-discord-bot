import asyncio
import re
import discord
from discord import app_commands
from discord.ext import commands

from database.account_links import (
    DiscordAccountAlreadyLinkedError,
    ProAvalonAccountAlreadyLinkedError,
    create_account_link,
    force_unlink_username,
    get_active_link_for_discord,
    get_active_link_for_username,
    get_saved_links_for_discord,
    get_linked_usernames,
    unlink_discord_account,
    repair_account_link_scopes_for_guild,
)

from database.seasons import (
    get_active_season,
    get_season_by_number,
    get_season_for_username,
    season_display_name,
)
from proavalon.profile_scraper import (
    ProAvalonError,
    ProAvalonLoginError,
    ProAvalonUserNotFound,
)

from utils.permissions import bot_admin_only

async def linked_username_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    if interaction.guild_id is None:
        return []

    usernames = await get_linked_usernames(
        interaction.guild_id
    )

    normalized_current = current.strip().casefold()

    matches = [
        username
        for username in usernames
        if normalized_current in username.casefold()
    ]

    return [
        app_commands.Choice(
            name=username,
            value=username,
        )
        for username in matches[:25]
    ]


async def resolve_proavalon_username(
    bot: commands.Bot,
    requested_username: str,
) -> str:
    """
    Validate a ProAvalon username and return its canonical casing.

    Account links are created before a season starts, so the username
    is validated directly against ProAvalon rather than against a
    season's registered-player list.
    """

    clean_username = requested_username.strip()

    if not clean_username:
        raise ValueError("ProAvalon username cannot be empty.")

    # Linking only requires that the ProAvalon account exists.  Do not
    # require profile statistics: brand-new zero-game accounts can link.
    return await bot.proavalon_scraper.resolve_username_case(
        clean_username
    )




async def resolve_link_season(
    guild_id: int,
    username: str,
    active_season,
):
    matched = await get_season_for_username(
        guild_id,
        username,
    )
    if matched is not None:
        return matched

    # Non-prefixed/general accounts retain the active season scope when one
    # exists, matching the bot's previous behavior without assuming that a
    # prefix number equals the internal season number.
    return active_season


async def link_scope_text(
    guild_id: int,
    season_number: int | None,
) -> str:
    if season_number is None:
        return "General"

    season = await get_season_by_number(
        guild_id,
        int(season_number),
    )
    if season is not None:
        return season_display_name(season)

    return f"Archived season scope {season_number}"



class AccountCommands(
    commands.GroupCog,
    group_name="account",
    group_description="Manage your ProAvalon account link.",
):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="link",
        description="Links your Discord account to a ProAvalon account.",
    )
    @app_commands.describe(
        proavalon_username="ProAvalon Username",
    )
    async def link(
        self,
        interaction: discord.Interaction,
        proavalon_username: str,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        await repair_account_link_scopes_for_guild(
            interaction.guild_id
        )
        season = await get_active_season(
            interaction.guild_id
        )

        await interaction.response.defer(
            thinking=True,
            ephemeral=True,
        )

        try:
            canonical_username = await resolve_proavalon_username(
                self.bot,
                proavalon_username,
            )

        except ProAvalonUserNotFound:
            await interaction.followup.send(
                (
                    f"Could not find the ProAvalon user "
                    f"`{proavalon_username.strip()}`."
                ),
                ephemeral=True,
            )
            return

        except ProAvalonLoginError:
            await interaction.followup.send(
                (
                    "The bot could not authenticate with ProAvalon. "
                    "An administrator should check the bot account."
                ),
                ephemeral=True,
            )
            return

        except asyncio.TimeoutError:
            await interaction.followup.send(
                (
                    "ProAvalon took too long to respond. "
                    "Please try again shortly."
                ),
                ephemeral=True,
            )
            return

        except ProAvalonError as error:
            await interaction.followup.send(
                f"Could not verify that ProAvalon account: {error}",
                ephemeral=True,
            )
            return

        except ValueError as error:
            await interaction.followup.send(
                str(error),
                ephemeral=True,
            )
            return

        target_season = await resolve_link_season(
            interaction.guild_id,
            canonical_username,
            season,
        )
        target_season_number = (
            int(target_season["season_number"])
            if target_season is not None
            else None
        )

        try:
            await create_account_link(
                guild_id=interaction.guild_id,
                discord_user_id=interaction.user.id,
                proavalon_username=canonical_username,
                season_number=target_season_number,
            )

        except (
            DiscordAccountAlreadyLinkedError,
            ProAvalonAccountAlreadyLinkedError,
        ) as error:
            await interaction.followup.send(
                str(error),
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            (
                f"Successfully linked `{canonical_username}` "
                f"to {interaction.user.mention} for "
                f"**{await link_scope_text(interaction.guild_id, target_season_number)}**. "
                "Links saved for other seasons are unchanged."
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="unlink",
        description="Unlinks your active ProAvalon account.",
    )
    async def unlink(
        self,
        interaction: discord.Interaction,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        await repair_account_link_scopes_for_guild(
            interaction.guild_id
        )
        season = await get_active_season(interaction.guild_id)
        season_number = (
            int(season["season_number"])
            if season is not None
            else None
        )
        if season_number is not None:
            preferred = await get_active_link_for_discord(
                interaction.guild_id,
                interaction.user.id,
                season_number,
            )
        else:
            preferred = await get_active_link_for_discord(
                interaction.guild_id,
                interaction.user.id,
            )

        if preferred is None:
            link = None
        else:
            link = await unlink_discord_account(
                interaction.guild_id,
                interaction.user.id,
                preferred["season_number"],
            )

        if link is None:
            await interaction.response.send_message(
                "You are not linked to a ProAvalon account.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            (
                f"Successfully unlinked from "
                f"`{link['proavalon_username']}`."
            )
        )

    @app_commands.command(
        name="status",
        description="Shows your current ProAvalon account link.",
    )
    async def status(
        self,
        interaction: discord.Interaction,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        await repair_account_link_scopes_for_guild(
            interaction.guild_id
        )
        links = await get_saved_links_for_discord(
            interaction.guild_id,
            interaction.user.id,
        )

        if not links:
            await interaction.response.send_message(
                "You are not linked to a ProAvalon account.",
                ephemeral=True,
            )
            return

        active_season = await get_active_season(interaction.guild_id)
        active_number = (
            int(active_season["season_number"])
            if active_season is not None
            else None
        )
        lines = []
        for link in links:
            link_season = link["season_number"]
            label = await link_scope_text(
                interaction.guild_id,
                link_season,
            )
            marker = (
                " **(active season)**"
                if active_number is not None
                and link_season == active_number
                else ""
            )
            lines.append(
                f"**{label}:** `{link['proavalon_username']}`{marker}"
            )

        await interaction.response.send_message(
            f"Saved links for {interaction.user.mention}:\n"
            + "\n".join(lines)
        )


class AdminAccountCommands(
    commands.GroupCog,
    group_name="admin-account",
    group_description="Administrative account-link commands.",
):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="link",
        description="Links a Discord user to a ProAvalon account.",
    )
    @app_commands.describe(
        discord_user="Discord @",
        proavalon_username="ProAvalon Username",
    )
    @bot_admin_only()
    async def admin_link(
        self,
        interaction: discord.Interaction,
        discord_user: discord.Member,
        proavalon_username: str,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        await repair_account_link_scopes_for_guild(
            interaction.guild_id
        )
        season = await get_active_season(
            interaction.guild_id
        )

        await interaction.response.defer(
            thinking=True,
            ephemeral=True,
        )

        try:
            canonical_username = await resolve_proavalon_username(
                self.bot,
                proavalon_username,
            )

        except ProAvalonUserNotFound:
            await interaction.followup.send(
                (
                    f"Could not find the ProAvalon user "
                    f"`{proavalon_username.strip()}`."
                ),
                ephemeral=True,
            )
            return

        except ProAvalonLoginError:
            await interaction.followup.send(
                (
                    "The bot could not authenticate with ProAvalon. "
                    "An administrator should check the bot account."
                ),
                ephemeral=True,
            )
            return

        except asyncio.TimeoutError:
            await interaction.followup.send(
                (
                    "ProAvalon took too long to respond. "
                    "Please try again shortly."
                ),
                ephemeral=True,
            )
            return

        except ProAvalonError as error:
            await interaction.followup.send(
                f"Could not verify that ProAvalon account: {error}",
                ephemeral=True,
            )
            return

        except ValueError as error:
            await interaction.followup.send(
                str(error),
                ephemeral=True,
            )
            return

        target_season = await resolve_link_season(
            interaction.guild_id,
            canonical_username,
            season,
        )
        target_season_number = (
            int(target_season["season_number"])
            if target_season is not None
            else None
        )

        try:
            await create_account_link(
                guild_id=interaction.guild_id,
                discord_user_id=discord_user.id,
                proavalon_username=canonical_username,
                season_number=target_season_number,
            )

        except (
            DiscordAccountAlreadyLinkedError,
            ProAvalonAccountAlreadyLinkedError,
        ) as error:
            await interaction.followup.send(
                str(error),
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            (
                f"Successfully linked `{canonical_username}` "
                f"to {discord_user.mention} for "
                f"**{await link_scope_text(interaction.guild_id, target_season_number)}**. "
                "Links saved for other seasons are unchanged."
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="force-unlink",
        description="Forcibly unlinks a ProAvalon account.",
    )
    @app_commands.describe(
        proavalon_username="ProAvalon Username",
    )
    @app_commands.autocomplete(
        proavalon_username=linked_username_autocomplete,
    )
    @bot_admin_only()
    async def force_unlink(
        self,
        interaction: discord.Interaction,
        proavalon_username: str,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        link = await force_unlink_username(
            interaction.guild_id,
            proavalon_username.strip(),
        )

        if link is None:
            await interaction.response.send_message(
                (
                    f"`{proavalon_username.strip()}` is not currently "
                    "linked to a Discord account."
                ),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            (
                f"Force-unlinked "
                f"`{link['proavalon_username']}` from "
                f"<@{link['discord_user_id']}>."
            )
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AccountCommands(bot))
    await bot.add_cog(AdminAccountCommands(bot))