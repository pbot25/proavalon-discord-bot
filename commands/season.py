import math

import discord
from discord import app_commands
from discord.ext import commands

from database.seasons import (
    count_season_players,
    count_voided_season_players,
    create_season,
    end_active_season,
    get_active_season,
    get_season_players,
    get_voided_season_players,
    reset_all_season_data,
    unvoid_season_player,
    void_season_player,
)


from database.ratings import (
    get_cached_rating,
    get_online_usernames,
)

from database.account_links import (
    get_registered_players_with_links,
)

from utils.permissions import bot_admin_only
from database.matches import (
    disable_match_channel,
    get_match_config,
    set_match_channel,
)


RANK_CHOICES = [
    app_commands.Choice(name="Unranked", value="unranked"),
    app_commands.Choice(name="Iron", value="iron"),
    app_commands.Choice(name="Bronze", value="bronze"),
    app_commands.Choice(name="Silver", value="silver"),
    app_commands.Choice(name="Gold", value="gold"),
    app_commands.Choice(name="Platinum", value="platinum"),
    app_commands.Choice(name="Diamond", value="diamond"),
    app_commands.Choice(name="Champion", value="champion"),
]

RANK_EMOJIS = {
    "unranked": "⚪",
    "iron": "⚙️",
    "bronze": "🥉",
    "silver": "🥈",
    "gold": "🥇",
    "platinum": "🔷",
    "diamond": "💎",
    "champion": "🏆",
}

STATUS_PAGE_SIZE = 15


class RosterStatusView(discord.ui.View):
    def __init__(
        self,
        *,
        title: str,
        lines: list[str],
        footer_label: str,
        requesting_user_id: int,
    ) -> None:
        super().__init__(timeout=180)

        self.title = title
        self.lines = lines
        self.footer_label = footer_label
        self.requesting_user_id = requesting_user_id
        self.page = 0
        self.total_pages = max(
            1,
            math.ceil(len(lines) / STATUS_PAGE_SIZE),
        )

        self._update_buttons()

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id != self.requesting_user_id:
            await interaction.response.send_message(
                "Only the person who opened this list can change pages.",
                ephemeral=True,
            )
            return False

        return True

    def _update_buttons(self) -> None:
        self.previous_button.disabled = self.page <= 0
        self.next_button.disabled = (
            self.page >= self.total_pages - 1
        )

    def build_embed(self) -> discord.Embed:
        start = self.page * STATUS_PAGE_SIZE
        end = start + STATUS_PAGE_SIZE
        page_lines = self.lines[start:end]

        embed = discord.Embed(
            title=self.title,
            description=(
                "\n".join(page_lines)
                if page_lines
                else "No matching players."
            ),
            color=discord.Color.blurple(),
        )

        embed.set_footer(
            text=(
                f"{len(self.lines)} {self.footer_label} • "
                f"Page {self.page + 1}/{self.total_pages}"
            )
        )

        return embed

    @discord.ui.button(
        label="Previous",
        emoji="◀️",
        style=discord.ButtonStyle.secondary,
    )
    async def previous_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if self.page > 0:
            self.page -= 1

        self._update_buttons()

        await interaction.response.edit_message(
            embed=self.build_embed(),
            view=self,
        )

    @discord.ui.button(
        label="Next",
        emoji="▶️",
        style=discord.ButtonStyle.secondary,
    )
    async def next_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if self.page < self.total_pages - 1:
            self.page += 1

        self._update_buttons()

        await interaction.response.edit_message(
            embed=self.build_embed(),
            view=self,
        )

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True


async def resolve_player_bracket(
    bot: commands.Bot,
    username: str,
) -> str | None:
    """
    Prefer the live/last-known socket bracket.

    If this bot has never observed the player online, fall back to the
    globally available profile-page rank already parsed by
    get_profile_data().
    """

    snapshot = get_cached_rating(username)

    if (
        snapshot is not None
        and snapshot.rating_bracket is not None
    ):
        return snapshot.rating_bracket.casefold()

    try:
        profile, _ = await bot.profile_cache.get_or_fetch(
            username,
            bot.proavalon_scraper.get_profile_data,
        )
    except Exception:
        return None

    if profile.rating_bracket is None:
        return None

    return profile.rating_bracket.casefold()


def build_status_line(
    index: int,
    username: str,
) -> str:
    snapshot = get_cached_rating(username)

    if snapshot is None:
        return (
            f"`{index:>2}.` `{username}` "
            "• **Rating: N/A**"
        )

    bracket = (
        snapshot.rating_bracket.casefold()
        if snapshot.rating_bracket
        else "unranked"
    )
    emoji = RANK_EMOJIS.get(bracket, "")
    emoji_prefix = f"{emoji} " if emoji else ""

    return (
        f"`{index:>2}.` {emoji_prefix}`{username}` "
        f"• **{snapshot.rating:,}**"
    )



class EndSeasonView(discord.ui.View):
    def __init__(
        self,
        requesting_user_id: int,
        season_number: int,
        guild_id: int,
    ) -> None:
        super().__init__(timeout=60)

        self.requesting_user_id = requesting_user_id
        self.season_number = season_number
        self.guild_id = guild_id

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id != self.requesting_user_id:
            await interaction.response.send_message(
                (
                    "Only the administrator who started this "
                    "confirmation can use these buttons."
                ),
                ephemeral=True,
            )
            return False

        return True

    @discord.ui.button(
        label="End Season",
        style=discord.ButtonStyle.danger,
    )
    async def confirm_end(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        season = await end_active_season(
            self.guild_id
        )

        if season is None:
            await interaction.response.edit_message(
                content="No community season is currently active.",
                view=None,
            )
            self.stop()
            return

        await interaction.response.edit_message(
            content=(
                f"Community Season {season['season_number']} "
                "has ended.\n"
                "Its registered player list remains archived.\n"
                "Daily leaderboard and automatic match posting "
                "have been disabled for this server."
            ),
            view=None,
        )

        self.stop()

    @discord.ui.button(
        label="Cancel",
        style=discord.ButtonStyle.secondary,
    )
    async def cancel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.edit_message(
            content="Season ending cancelled.",
            view=None,
        )

        self.stop()

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True


class ResetSeasonsView(discord.ui.View):
    def __init__(
        self,
        requesting_user_id: int,
        guild_id: int,
    ) -> None:
        super().__init__(timeout=60)

        self.requesting_user_id = requesting_user_id
        self.guild_id = guild_id

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id != self.requesting_user_id:
            await interaction.response.send_message(
                (
                    "Only the administrator who started this "
                    "confirmation can use these buttons."
                ),
                ephemeral=True,
            )
            return False

        return True

    @discord.ui.button(
        label="Confirm Reset",
        style=discord.ButtonStyle.danger,
    )
    async def confirm_reset(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await reset_all_season_data(
            self.guild_id
        )

        await interaction.response.edit_message(
            content=(
                "All community seasons and registered players "
                "for this server have been permanently deleted.\n"
                "This server is now back to having no season history.\n"
                "Daily leaderboard and automatic match posting "
                "have been disabled for this server."
            ),
            view=None,
        )

        self.stop()

    @discord.ui.button(
        label="Cancel",
        style=discord.ButtonStyle.secondary,
    )
    async def cancel_reset(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.edit_message(
            content="Season reset cancelled.",
            view=None,
        )

        self.stop()

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True


REGISTERED_PLAYERS_PAGE_SIZE = 15


def build_registered_players_page(
    players,
    page: int,
) -> str:
    """Build one page of registered players."""

    start = page * REGISTERED_PLAYERS_PAGE_SIZE
    end = start + REGISTERED_PLAYERS_PAGE_SIZE
    page_players = players[start:end]

    if not page_players:
        return "No registered players."

    max_username_length = max(
        len(player["proavalon_username"])
        for player in page_players
    )

    lines = []

    for index, player in enumerate(
        page_players,
        start=start + 1,
    ):
        username = player["proavalon_username"]
        discord_user_id = player["discord_user_id"]

        line = (
            f"`{index:>2}.` "
            f"`{username:<{max_username_length}}`"
        )

        if discord_user_id is not None:
            line += f" <@{discord_user_id}>"

        lines.append(line)

    return "\n".join(lines)


class RegisteredPlayersView(discord.ui.View):
    def __init__(
        self,
        *,
        players,
        season_number: int,
        requesting_user_id: int,
    ) -> None:
        super().__init__(timeout=180)

        self.players = players
        self.season_number = season_number
        self.requesting_user_id = requesting_user_id

        self.page = 0
        self.total_pages = max(
            1,
            math.ceil(
                len(players)
                / REGISTERED_PLAYERS_PAGE_SIZE
            ),
        )

        self._update_buttons()

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id != self.requesting_user_id:
            await interaction.response.send_message(
                (
                    "Only the person who opened this list "
                    "can change pages."
                ),
                ephemeral=True,
            )
            return False

        return True

    def _update_buttons(self) -> None:
        self.previous_button.disabled = self.page <= 0
        self.next_button.disabled = (
            self.page >= self.total_pages - 1
        )

    def build_embed(self) -> discord.Embed:
        start = (
            self.page * REGISTERED_PLAYERS_PAGE_SIZE
            + 1
        )

        end = min(
            (self.page + 1)
            * REGISTERED_PLAYERS_PAGE_SIZE,
            len(self.players),
        )

        embed = discord.Embed(
            title=(
                f"Community Season "
                f"{self.season_number} — Registered Players"
            ),
            description=build_registered_players_page(
                players=self.players,
                page=self.page,
            ),
            color=discord.Color.blurple(),
        )

        embed.set_footer(
            text=(
                f"Showing {start}–{end} of "
                f"{len(self.players)} registered player(s) • "
                f"Page {self.page + 1}/{self.total_pages}"
            )
        )

        return embed

    @discord.ui.button(
        label="Previous",
        emoji="◀️",
        style=discord.ButtonStyle.secondary,
    )
    async def previous_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if self.page > 0:
            self.page -= 1

        self._update_buttons()

        await interaction.response.edit_message(
            embed=self.build_embed(),
            view=self,
        )

    @discord.ui.button(
        label="Next",
        emoji="▶️",
        style=discord.ButtonStyle.secondary,
    )
    async def next_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if self.page < self.total_pages - 1:
            self.page += 1

        self._update_buttons()

        await interaction.response.edit_message(
            embed=self.build_embed(),
            view=self,
        )

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True


async def voidable_season_player_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    """
    Suggest currently registered players for convenience.

    Admins may still type an eligible season username manually even if it
    has never registered yet.
    """

    if interaction.guild_id is None:
        return []

    season = await get_active_season(
        interaction.guild_id
    )

    if season is None:
        return []

    players = await get_season_players(
        season["id"]
    )
    voided = await get_voided_season_players(
        season["id"]
    )

    voided_names = {
        str(player["proavalon_username"]).casefold()
        for player in voided
    }

    normalized = current.strip().casefold()

    matches = [
        str(player["proavalon_username"])
        for player in players
        if (
            str(player["proavalon_username"]).casefold()
            not in voided_names
            and normalized
            in str(
                player["proavalon_username"]
            ).casefold()
        )
    ]

    return [
        app_commands.Choice(
            name=username,
            value=username,
        )
        for username in matches[:25]
    ]


async def voided_season_player_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    if interaction.guild_id is None:
        return []

    season = await get_active_season(
        interaction.guild_id
    )

    if season is None:
        return []

    players = await get_voided_season_players(
        season["id"]
    )
    normalized = current.strip().casefold()

    matches = [
        str(player["proavalon_username"])
        for player in players
        if normalized
        in str(
            player["proavalon_username"]
        ).casefold()
    ]

    return [
        app_commands.Choice(
            name=username,
            value=username,
        )
        for username in matches[:25]
    ]


VOID_LIST_PAGE_SIZE = 15


class SeasonVoidListView(discord.ui.View):
    def __init__(
        self,
        *,
        players,
        season_number: int,
        requesting_user_id: int,
    ) -> None:
        super().__init__(timeout=180)

        self.players = players
        self.season_number = season_number
        self.requesting_user_id = requesting_user_id

        self.page = 0
        self.total_pages = max(
            1,
            math.ceil(
                len(players)
                / VOID_LIST_PAGE_SIZE
            ),
        )

        self._update_buttons()

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id != self.requesting_user_id:
            await interaction.response.send_message(
                (
                    "Only the person who opened this list "
                    "can change pages."
                ),
                ephemeral=True,
            )
            return False

        return True

    def _update_buttons(self) -> None:
        self.previous_button.disabled = self.page <= 0
        self.next_button.disabled = (
            self.page >= self.total_pages - 1
        )

    def build_embed(self) -> discord.Embed:
        start = self.page * VOID_LIST_PAGE_SIZE
        end = start + VOID_LIST_PAGE_SIZE
        page_players = self.players[start:end]

        lines = [
            (
                f"`{index:>2}.` "
                f"`{player['proavalon_username']}`"
            )
            for index, player in enumerate(
                page_players,
                start=start + 1,
            )
        ]

        description = (
            "\n".join(lines)
            if lines
            else "No players are currently voided."
        )

        embed = discord.Embed(
            title=(
                f"Community Season "
                f"{self.season_number} — Void List"
            ),
            description=description,
            color=discord.Color.red(),
        )

        if self.players:
            shown_start = start + 1
            shown_end = min(
                end,
                len(self.players),
            )

            footer = (
                f"Showing {shown_start}–{shown_end} of "
                f"{len(self.players)} voided player(s) • "
                f"Page {self.page + 1}/{self.total_pages}"
            )
        else:
            footer = "0 voided players"

        embed.set_footer(text=footer)

        return embed

    @discord.ui.button(
        label="Previous",
        emoji="◀️",
        style=discord.ButtonStyle.secondary,
    )
    async def previous_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if self.page > 0:
            self.page -= 1

        self._update_buttons()

        await interaction.response.edit_message(
            embed=self.build_embed(),
            view=self,
        )

    @discord.ui.button(
        label="Next",
        emoji="▶️",
        style=discord.ButtonStyle.secondary,
    )
    async def next_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if self.page < self.total_pages - 1:
            self.page += 1

        self._update_buttons()

        await interaction.response.edit_message(
            embed=self.build_embed(),
            view=self,
        )

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True


class SeasonCommands(
    commands.GroupCog,
    group_name="season",
    group_description="Manage the community season.",
):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot


    @app_commands.command(
        name="match-channel",
        description="Sets the channel for automatic active-season match results.",
    )
    @app_commands.describe(channel="Channel where qualifying season matches will be posted.")
    @bot_admin_only()
    async def match_channel(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return
        await set_match_channel(interaction.guild_id, channel.id)
        await interaction.response.send_message(
            f"Automatic season match results will be posted in {channel.mention}.\n"
            "Matches are detected from the active season prefix and do not require players to be registered first.",
            ephemeral=True,
        )

    @app_commands.command(
        name="match-channel-info",
        description="Shows this server's automatic season match channel.",
    )
    async def match_channel_info(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return
        config = await get_match_config(interaction.guild_id)
        if config is None or not config["enabled"]:
            await interaction.response.send_message("Automatic season match posting is disabled in this server.", ephemeral=True)
            return
        await interaction.response.send_message(f"Automatic season matches are posted in <#{config['channel_id']}>.", ephemeral=True)

    @app_commands.command(
        name="match-channel-disable",
        description="Disables automatic season match posting in this server.",
    )
    @bot_admin_only()
    async def match_channel_disable(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return
        disabled = await disable_match_channel(interaction.guild_id)
        await interaction.response.send_message(
            "Automatic season match posting has been disabled." if disabled else "Automatic season match posting was already disabled.",
            ephemeral=True,
        )

    @app_commands.command(
        name="online-players",
        description="Shows registered season players currently online on ProAvalon.",
    )
    async def online_players(
        self,
        interaction: discord.Interaction,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        season = await get_active_season(
            interaction.guild_id
        )

        if season is None:
            await interaction.response.send_message(
                "No community season is currently active.",
                ephemeral=True,
            )
            return

        players = await get_season_players(
            season["id"]
        )
        online = get_online_usernames()

        matching = [
            str(player["proavalon_username"])
            for player in players
            if str(
                player["proavalon_username"]
            ).casefold() in online
        ]

        matching.sort(
            key=lambda username: (
                -(
                    get_cached_rating(username).rating
                    if get_cached_rating(username) is not None
                    else -1
                ),
                username.casefold(),
            )
        )

        if not matching:
            await interaction.response.send_message(
                (
                    f"No registered Community Season "
                    f"{season['season_number']} players are currently online."
                )
            )
            return

        lines = [
            build_status_line(index, username)
            for index, username in enumerate(
                matching,
                start=1,
            )
        ]

        view = RosterStatusView(
            title=(
                f"Community Season "
                f"{season['season_number']} — Online Players"
            ),
            lines=lines,
            footer_label="online season player(s)",
            requesting_user_id=interaction.user.id,
        )

        await interaction.response.send_message(
            embed=view.build_embed(),
            view=view,
        )

    @app_commands.command(
        name="online-ping",
        description="Pings linked season players who are currently online on ProAvalon.",
    )
    @app_commands.describe(
        message=(
            "Optional lobby message. Defaults to a short join-lobby message."
        ),
    )
    @bot_admin_only()
    async def online_ping(
        self,
        interaction: discord.Interaction,
        message: app_commands.Range[str, 1, 1000] | None = None,
    ) -> None:
        if interaction.guild_id is None or interaction.guild is None:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        season = await get_active_season(
            interaction.guild_id
        )

        if season is None:
            await interaction.response.send_message(
                "No community season is currently active.",
                ephemeral=True,
            )
            return

        linked_players = await get_registered_players_with_links(
            interaction.guild_id,
            int(season["id"]),
        )
        online_usernames = get_online_usernames()

        # Keep only active-season players who are BOTH online on ProAvalon
        # and linked to a Discord user for this same server/season.
        candidates = [
            row
            for row in linked_players
            if (
                row["discord_user_id"] is not None
                and str(row["proavalon_username"]).casefold()
                in online_usernames
            )
        ]

        if not candidates:
            await interaction.response.send_message(
                (
                    "No currently online season players have a linked "
                    "Discord account in this server."
                ),
                ephemeral=True,
            )
            return

        # Confirm the linked Discord account is still a member of this server.
        # This prevents stale season links from producing raw mentions after a
        # user has left the server.
        members: list[discord.Member] = []
        seen_discord_ids: set[int] = set()

        for row in candidates:
            try:
                discord_user_id = int(row["discord_user_id"])
            except (TypeError, ValueError):
                continue

            if discord_user_id in seen_discord_ids:
                continue

            member = interaction.guild.get_member(
                discord_user_id
            )

            if member is None:
                try:
                    member = await interaction.guild.fetch_member(
                        discord_user_id
                    )
                except (
                    discord.NotFound,
                    discord.Forbidden,
                    discord.HTTPException,
                ):
                    continue

            if member.bot:
                continue

            seen_discord_ids.add(discord_user_id)
            members.append(member)

        if not members:
            await interaction.response.send_message(
                (
                    "Online linked season players were found, but none of "
                    "their linked Discord accounts are currently in this server."
                ),
                ephemeral=True,
            )
            return

        mentions = " ".join(
            member.mention
            for member in members
        )

        lobby_message = (
            message.strip()
            if message is not None
            else "A ProAvalon lobby is forming — join the lobby!"
        )

        content = (
            f"{mentions}\n\n"
            f"{lobby_message}"
        )

        if len(content) > 2000:
            await interaction.response.send_message(
                (
                    "The ping message would exceed Discord's 2,000-character "
                    "message limit. Use a shorter custom message."
                ),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            content,
            allowed_mentions=discord.AllowedMentions(
                everyone=False,
                roles=False,
                users=True,
                replied_user=False,
            ),
        )

    @app_commands.command(
        name="rank-players",
        description="Shows active-season players under a specific ProAvalon rank.",
    )
    @app_commands.describe(
        rank="Rank/tier to filter the active season by.",
    )
    @app_commands.choices(
        rank=RANK_CHOICES,
    )
    async def rank_players(
        self,
        interaction: discord.Interaction,
        rank: app_commands.Choice[str],
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        season = await get_active_season(
            interaction.guild_id
        )

        if season is None:
            await interaction.response.send_message(
                "No community season is currently active.",
                ephemeral=True,
            )
            return

        players = await get_season_players(
            season["id"]
        )

        await interaction.response.defer(
            thinking=True,
        )

        matching: list[str] = []

        for player in players:
            username = str(
                player["proavalon_username"]
            )

            bracket = await resolve_player_bracket(
                self.bot,
                username,
            )

            if bracket == rank.value:
                matching.append(username)

        matching.sort(
            key=lambda username: (
                -(
                    get_cached_rating(username).rating
                    if get_cached_rating(username) is not None
                    else -1
                ),
                username.casefold(),
            )
        )

        if not matching:
            await interaction.followup.send(
                (
                    f"No Community Season "
                    f"{season['season_number']} players are "
                    f"currently classified as **{rank.name}**."
                )
            )
            return

        lines = [
            build_status_line(index, username)
            for index, username in enumerate(
                matching,
                start=1,
            )
        ]

        view = RosterStatusView(
            title=(
                f"Community Season "
                f"{season['season_number']} — {rank.name}"
            ),
            lines=lines,
            footer_label=f"{rank.name} player(s)",
            requesting_user_id=interaction.user.id,
        )

        await interaction.followup.send(
            embed=view.build_embed(),
            view=view,
        )

    @app_commands.command(
        name="info",
        description="Shows the active community season.",
    )
    async def info(
        self,
        interaction: discord.Interaction,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        season = await get_active_season(
            interaction.guild_id
        )

        if season is None:
            await interaction.response.send_message(
                "No community season is currently active."
            )
            return

        player_count = await count_season_players(
            season["id"]
        )

        embed = discord.Embed(
            title=f"Community Season {season['season_number']}",
            color=discord.Color.blurple(),
        )

        embed.add_field(
            name="Required prefix",
            value=f"`{season['required_prefix']}`",
            inline=True,
        )

        embed.add_field(
            name="Tracked game size",
            value=f"{season['tracked_player_count']} players",
            inline=True,
        )

        embed.add_field(
            name="Registered players",
            value=f"{player_count:,}",
            inline=True,
        )

        voided_count = await count_voided_season_players(
            season["id"]
        )

        embed.add_field(
            name="Voided players",
            value=f"{voided_count:,}",
            inline=True,
        )

        embed.add_field(
            name="Started",
            value=season["started_at"][:10],
            inline=True,
        )

        await interaction.response.send_message(embed=embed)

    @app_commands.command(
        name="registered-players",
        description="Shows all players registered for the active community season.",
    )
    async def registered_players(
        self,
        interaction: discord.Interaction,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        season = await get_active_season(
            interaction.guild_id
        )

        if season is None:
            await interaction.response.send_message(
                "No community season is currently active.",
                ephemeral=True,
            )
            return

        players = await get_registered_players_with_links(
            interaction.guild_id,
            season["id"],
        )

        if not players:
            await interaction.response.send_message(
                (
                    f"No players are currently registered for "
                    f"Community Season {season['season_number']}."
                )
            )
            return

        view = RegisteredPlayersView(
            players=players,
            season_number=int(
                season["season_number"]
            ),
            requesting_user_id=interaction.user.id,
        )

        await interaction.response.send_message(
            embed=view.build_embed(),
            view=view,
        )

    @app_commands.command(
        name="void-list",
        description="Lists all players voided from the active season.",
    )
    async def void_list(
        self,
        interaction: discord.Interaction,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        season = await get_active_season(
            interaction.guild_id
        )

        if season is None:
            await interaction.response.send_message(
                "No community season is currently active.",
                ephemeral=True,
            )
            return

        voided_players = await get_voided_season_players(
            season["id"]
        )

        if not voided_players:
            await interaction.response.send_message(
                (
                    f"No players are currently voided from "
                    f"Community Season "
                    f"{season['season_number']}."
                )
            )
            return

        view = SeasonVoidListView(
            players=voided_players,
            season_number=int(
                season["season_number"]
            ),
            requesting_user_id=interaction.user.id,
        )

        await interaction.response.send_message(
            embed=view.build_embed(),
            view=view,
        )

    @app_commands.command(
        name="void-player",
        description="Excludes a ProAvalon account from the active season.",
    )
    @app_commands.describe(
        username=(
            "Season username to exclude. "
            "Registered players are suggested automatically."
        ),
    )
    @app_commands.autocomplete(
        username=voidable_season_player_autocomplete,
    )
    @bot_admin_only()
    async def void_player(
        self,
        interaction: discord.Interaction,
        username: str,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        season = await get_active_season(
            interaction.guild_id
        )

        if season is None:
            await interaction.response.send_message(
                "No community season is currently active.",
                ephemeral=True,
            )
            return

        clean_username = username.strip()

        if not clean_username:
            await interaction.response.send_message(
                "Enter a ProAvalon username.",
                ephemeral=True,
            )
            return

        required_prefix = str(
            season["required_prefix"]
        )

        if not clean_username.casefold().startswith(
            required_prefix.casefold()
        ):
            await interaction.response.send_message(
                (
                    f"`{clean_username}` does not begin with "
                    f"the active season prefix "
                    f"`{required_prefix}`."
                ),
                ephemeral=True,
            )
            return

        created = await void_season_player(
            community_season_id=int(
                season["id"]
            ),
            proavalon_username=clean_username,
            voided_by_discord_user_id=(
                interaction.user.id
            ),
        )

        if not created:
            await interaction.response.send_message(
                (
                    f"`{clean_username}` is already voided from "
                    f"Community Season "
                    f"{season['season_number']}."
                ),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            (
                f"`{clean_username}` has been voided from "
                f"Community Season "
                f"{season['season_number']}.\n"
                "It has been removed from the registered-player "
                "roster and will not be automatically registered "
                "by `/stats player` while it remains voided."
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="unvoid-player",
        description="Allows a voided account to join the active season again.",
    )
    @app_commands.describe(
        username="Voided season username to restore.",
    )
    @app_commands.autocomplete(
        username=voided_season_player_autocomplete,
    )
    @bot_admin_only()
    async def unvoid_player(
        self,
        interaction: discord.Interaction,
        username: str,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        season = await get_active_season(
            interaction.guild_id
        )

        if season is None:
            await interaction.response.send_message(
                "No community season is currently active.",
                ephemeral=True,
            )
            return

        clean_username = username.strip()

        removed = await unvoid_season_player(
            community_season_id=int(
                season["id"]
            ),
            proavalon_username=clean_username,
        )

        if removed is None:
            await interaction.response.send_message(
                (
                    f"`{clean_username}` is not voided from "
                    f"Community Season "
                    f"{season['season_number']}."
                ),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            (
                f"`{removed['proavalon_username']}` is no longer "
                f"voided from Community Season "
                f"{season['season_number']}.\n"
                "The account can register again the next time "
                "`/stats player` is used for it and it meets "
                "the normal season requirements."
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="set",
        description="Starts a new 10-player community season.",
    )
    @app_commands.describe(
        prefix="Required account prefix, such as S1, r1, or ABC.",
    )
    @bot_admin_only()
    async def set_season(
        self,
        interaction: discord.Interaction,
        prefix: str,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        clean_prefix = prefix.strip()

        if (
            not clean_prefix
            or len(clean_prefix) > 20
            or not clean_prefix.isalnum()
        ):
            await interaction.response.send_message(
                (
                    "Enter an alphanumeric prefix between 1 and 20 "
                    "characters, for example `S1`, `r1`, or `ABC`."
                ),
                ephemeral=True,
            )
            return

        try:
            season = await create_season(
                interaction.guild_id,
                clean_prefix,
            )
        except (RuntimeError, ValueError) as error:
            await interaction.response.send_message(
                str(error),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            (
                f"Community Season {season['season_number']} "
                "is now active.\n"
                f"Only ProAvalon usernames beginning with "
                f"`{season['required_prefix']}` are eligible.\n"
                "Eligible accounts must have at least one "
                "10-player game."
            )
        )

    @app_commands.command(
        name="end",
        description="Ends and archives the active community season.",
    )
    @bot_admin_only()
    async def end_season(
        self,
        interaction: discord.Interaction,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        season = await get_active_season(
            interaction.guild_id
        )

        if season is None:
            await interaction.response.send_message(
                "No community season is currently active.",
                ephemeral=True,
            )
            return

        view = EndSeasonView(
            requesting_user_id=interaction.user.id,
            season_number=season["season_number"],
            guild_id=interaction.guild_id,
        )

        await interaction.response.send_message(
            (
                f"Are you sure you want to end Community "
                f"Season {season['season_number']}?\n"
                "The season and its registered players "
                "will remain archived."
            ),
            view=view,
            ephemeral=True,
        )

    @app_commands.command(
        name="reset",
        description=(
            "Deletes all community seasons and registered players "
            "(testing only)."
        ),
    )
    @app_commands.describe(
        confirmation='Type exactly: "I understand"',
    )
    @bot_admin_only()
    async def reset_seasons(
        self,
        interaction: discord.Interaction,
        confirmation: str,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        required_phrase = "I understand"

        if confirmation.strip() != required_phrase:
            await interaction.response.send_message(
                (
                    "❌ Reset cancelled.\n\n"
                    f'To continue, you must type exactly: '
                    f'**"{required_phrase}"**'
                ),
                ephemeral=True,
            )
            return

        view = ResetSeasonsView(
            requesting_user_id=interaction.user.id,
            guild_id=interaction.guild_id,
        )

        await interaction.response.send_message(
            (
                "⚠️ **Final confirmation required**\n\n"
                "You typed the correct confirmation phrase.\n\n"
                "Click **Confirm Reset** below if you are absolutely sure.\n\n"
                "This will permanently delete:\n"
                "• every community season in this server\n"
                "• every registered player in this server\n\n"
                "**Bot administrators and account links for this server will NOT be deleted.**"
            ),
            view=view,
            ephemeral=True,
        )

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.CheckFailure):
            message = (
                "You do not have bot administrator permission."
            )

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


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SeasonCommands(bot))