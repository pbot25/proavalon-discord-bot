import math

import discord
from discord import app_commands
from discord.ext import commands

from database.seasons import (
    count_season_players,
    count_voided_season_players,
    cleanup_ended_season_operational_data,
    create_season,
    end_active_season,
    fetch_season_history,
    rename_active_season,
    season_display_name,
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
    get_unlinked_registered_usernames,
)

from utils.permissions import bot_admin_only
from database.matches import (
    disable_match_channel,
    get_match_config,
    set_match_channel,
)
from database.insights import season_overview_summary, season_rows
from services.advanced_stats_backfill import backfill_advanced_stats


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


def select_next_waiting_lobby(
    lobbies: list[dict],
    registered_usernames: set[str] | None = None,
) -> tuple[int | None, set[str]]:
    """Choose the most populated public non-game lobby with a known roster."""
    candidates: list[tuple[int, int, int, set[str]]] = []
    for lobby in lobbies:
        status = str(lobby.get("status") or "").casefold()
        if status in {"game in progress", "finished"}:
            continue
        if bool(lobby.get("password_locked", False)):
            continue
        players = {
            str(username).strip().casefold()
            for username in lobby.get("players") or []
            if isinstance(username, str) and username.strip()
        }
        if not players:
            continue
        try:
            room_id = int(lobby["room_id"])
        except (KeyError, TypeError, ValueError):
            continue
        registered_count = (
            len(players.intersection(registered_usernames))
            if registered_usernames is not None
            else len(players)
        )
        if registered_usernames is not None and registered_count == 0:
            continue
        candidates.append((registered_count, len(players), room_id, players))

    if not candidates:
        return None, set()

    _, _, room_id, players = max(
        candidates, key=lambda item: (item[0], item[1], item[2])
    )
    return room_id, players


def format_season_duration(seconds) -> str:
    if seconds is None:
        return "N/A"
    total = max(0, int(round(float(seconds))))
    hours, remainder = divmod(total, 3600)
    minutes, remaining_seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {remaining_seconds}s"
    return f"{minutes}m {remaining_seconds}s"


def format_outcome_split(values) -> str:
    """Format [games, Resistance wins, Spy wins] for conditional stats."""
    games, resistance_wins, spy_wins = (int(value) for value in values)
    known = resistance_wins + spy_wins
    resistance_rate = resistance_wins / known * 100 if known else 0.0
    return (
        f"{games:,} games • Res WR {resistance_rate:.1f}% "
        f"({resistance_wins:,}/{games:,})"
    )


def format_storage_size(size_bytes: int) -> str:
    value = float(max(0, size_bytes))
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"


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
        season_name: str,
        guild_id: int,
    ) -> None:
        super().__init__(timeout=60)

        self.requesting_user_id = requesting_user_id
        self.season_name = season_name
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
        await interaction.response.edit_message(
            content=(
                f"Ending **{self.season_name}** and cleaning its operational "
                "recording logs…"
            ),
            view=None,
        )
        self.stop()

        season = await end_active_season(self.guild_id)

        if season is None:
            await interaction.edit_original_response(
                content="No community season is currently active.",
            )
            return

        try:
            cleanup = await cleanup_ended_season_operational_data(
                self.guild_id,
                int(season["id"]),
                str(season["started_at"]),
            )
            cleanup_message = (
                f"Removed **{cleanup['deleted_audit_events']:,}** operational "
                "recording audit events and compacted "
                f"**{cleanup['compacted_matches']:,}** match records. "
            )
            if cleanup["compacted"]:
                cleanup_message += (
                    "The database was compacted"
                    f" (**{format_storage_size(cleanup['bytes_reclaimed'])}** "
                    "physically reclaimed)."
                )
            else:
                cleanup_message += (
                    "The freed database pages are available for SQLite to reuse; "
                    "physical compaction could not run right now."
                )
        except Exception as error:
            cleanup_message = (
                "The season ended successfully, but operational-log cleanup "
                f"could not finish: `{error}`"
            )

        await interaction.edit_original_response(
            content=(
                f"{season_display_name(season)} "
                "has ended.\n"
                "Its searchable matches, teams/roles, results, durations, missions, "
                "season account links, and YouTube links remain available. Detailed "
                "vote history, rating-change payloads, recording logs/statuses, and "
                "obsolete void-list entries were removed.\n"
                "Daily leaderboards and automatic match-history posting are "
                "preserved. They will resume automatically when the next season "
                "starts.\n"
                f"{cleanup_message}"
            ),
        )

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
        season_name: str,
        requesting_user_id: int,
    ) -> None:
        super().__init__(timeout=180)

        self.players = players
        self.season_name = season_name
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
            title=f"{self.season_name} — Registered Players",
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
        season_name: str,
        requesting_user_id: int,
    ) -> None:
        super().__init__(timeout=180)

        self.players = players
        self.season_name = season_name
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
            title=f"{self.season_name} — Void List",
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
                    f"No registered players in {season_display_name(season)} are currently online."
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
                f"{season_display_name(season)} — Online Players"
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
        name="next-lobby-ping",
        description="Pings registered voice members available for the next lobby.",
    )
    @app_commands.describe(
        voice_channel=(
            "Voice channel to check. Defaults to the channel you are currently in."
        ),
        message=(
            "Optional message. Defaults to an invitation to join the next lobby."
        ),
    )
    @bot_admin_only()
    async def next_lobby_ping(
        self,
        interaction: discord.Interaction,
        voice_channel: discord.VoiceChannel | None = None,
        message: app_commands.Range[str, 1, 1000] | None = None,
    ) -> None:
        if interaction.guild_id is None or interaction.guild is None:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        season = await get_active_season(interaction.guild_id)
        if season is None:
            await interaction.response.send_message(
                "No community season is currently active.",
                ephemeral=True,
            )
            return

        target_channel = voice_channel
        if target_channel is None:
            member = interaction.user
            voice_state = (
                member.voice
                if isinstance(member, discord.Member)
                else None
            )
            current_channel = voice_state.channel if voice_state else None
            if isinstance(current_channel, discord.VoiceChannel):
                target_channel = current_channel

        if target_channel is None:
            await interaction.response.send_message(
                (
                    "Join a voice channel first, or select one with the "
                    "`voice_channel` option."
                ),
                ephemeral=True,
            )
            return

        if target_channel.guild.id != interaction.guild_id:
            await interaction.response.send_message(
                "Choose a voice channel from this server.",
                ephemeral=True,
            )
            return

        leaderboard_cog = self.bot.get_cog("LeaderboardCommands")
        tracker = getattr(leaderboard_cog, "rating_tracker", None)
        if tracker is None:
            await interaction.response.send_message(
                "The live ProAvalon match tracker is not ready right now.",
                ephemeral=True,
            )
            return

        linked_players = await get_registered_players_with_links(
            interaction.guild_id,
            int(season["id"]),
        )
        linked_username_by_discord_id = {
            int(row["discord_user_id"]): str(row["proavalon_username"]).casefold()
            for row in linked_players
            if row["discord_user_id"] is not None
        }
        lobby_snapshot = await tracker.get_fresh_recorder_lobby_snapshot()
        ongoing_usernames = {
            str(username).casefold()
            for lobby in lobby_snapshot
            if str(lobby.get("status") or "").casefold() == "game in progress"
            for username in lobby.get("players") or []
            if isinstance(username, str)
        }
        waiting_room_id, waiting_usernames = select_next_waiting_lobby(
            lobby_snapshot,
            set(linked_username_by_discord_id.values()),
        )

        eligible_members: list[discord.Member] = []
        unregistered_count = 0
        playing_count = 0
        waiting_count = 0
        for member in target_channel.members:
            if member.bot:
                continue
            username = linked_username_by_discord_id.get(member.id)
            if username is None:
                unregistered_count += 1
                continue
            if username in ongoing_usernames:
                playing_count += 1
                continue
            if waiting_room_id is not None and username in waiting_usernames:
                waiting_count += 1
                continue
            eligible_members.append(member)

        if not eligible_members:
            await interaction.response.send_message(
                (
                    f"Nobody in {target_channel.mention} is available to ping. "
                    f"Excluded {playing_count} playing, {waiting_count} already "
                    f"in the waiting lobby, and {unregistered_count} unregistered."
                ),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        lobby_message = (
            message.strip()
            if message is not None
            else "You are available for the next ProAvalon lobby — join when it opens!"
        )
        mention_chunks: list[str] = []
        current_mentions: list[str] = []
        current_length = len(lobby_message) + 2
        for member in eligible_members:
            mention = member.mention
            added_length = len(mention) + (1 if current_mentions else 0)
            if current_mentions and current_length + added_length > 1750:
                mention_chunks.append(" ".join(current_mentions))
                current_mentions = []
                current_length = len(lobby_message) + 2
            current_mentions.append(mention)
            current_length += added_length
        if current_mentions:
            mention_chunks.append(" ".join(current_mentions))

        allowed_mentions = discord.AllowedMentions(
            everyone=False,
            roles=False,
            users=True,
            replied_user=False,
        )
        summary = (
            f"{lobby_message}\n\n{mention_chunks[0]}\n\n"
            f"*Checked {target_channel.mention}: {len(eligible_members)} available, "
            f"{playing_count} playing, {waiting_count} already in "
            + (
                f"waiting room {waiting_room_id}, "
                if waiting_room_id is not None
                else "a waiting lobby (none detected), "
            )
            + f"{unregistered_count} unregistered.*"
        )
        await interaction.response.send_message(
            summary,
            allowed_mentions=allowed_mentions,
        )
        for chunk in mention_chunks[1:]:
            await interaction.followup.send(
                chunk,
                allowed_mentions=allowed_mentions,
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
                    f"No players in {season_display_name(season)} are currently classified as **{rank.name}**."
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
                f"{season_display_name(season)} — {rank.name}"
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
        name="refresh-stats",
        description="Refreshes every registered player's current website statistics.",
    )
    @bot_admin_only()
    async def refresh_stats(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return

        leaderboard_cog = self.bot.get_cog("LeaderboardCommands")
        refresh = getattr(leaderboard_cog, "refresh_current_season_profiles", None)
        if refresh is None:
            await interaction.response.send_message(
                "The website-stat synchronization service is not ready.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True, ephemeral=True)
        season, profiles, refreshed, failed = await refresh(
            interaction.guild_id,
            allow_recent_reuse=False,
        )
        if season is None:
            await interaction.followup.send(
                "No community season is currently active.", ephemeral=True
            )
            return

        await interaction.followup.send(
            (
                f"Refreshed website statistics for "
                f"**{season_display_name(season)}**.\n"
                f"Successfully refreshed: **{refreshed:,}**\n"
                f"Failed website requests: **{failed:,}**\n"
                f"Profiles now available: **{len(profiles):,}**\n\n"
                "Games played, win rates, role records, alignment records, "
                "rank, and total play time now use the refreshed profiles."
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="info",
        description="Shows current-season configuration and overall statistics.",
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
            title=f"{season_display_name(season)}",
            description="**Active season**",
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

        rows = await season_rows(int(season["id"]))
        summary = season_overview_summary(rows)
        result_lines = [
            (
                f"**Resistance:** {summary['resistance_wins']:,} wins • "
                f"**{summary['resistance_win_rate']:.1f}%**"
            ),
            (
                f"**Spies:** {summary['spy_wins']:,} wins • "
                f"**{summary['spy_win_rate']:.1f}%**"
            ),
        ]
        if summary["unknown_results"]:
            result_lines.append(
                f"Unknown/unresolved: **{summary['unknown_results']:,}**"
            )
        embed.add_field(
            name=f"Season Results • {summary['matches']:,} matches",
            value="\n".join(result_lines),
            inline=False,
        )

        shortest_match = (
            f" • Match #{summary['shortest_match_id']}"
            if summary["shortest_match_id"] is not None
            else ""
        )
        longest_match = (
            f" • Match #{summary['longest_match_id']}"
            if summary["longest_match_id"] is not None
            else ""
        )
        embed.add_field(
            name="Game Lengths",
            value=(
                f"**Average:** {format_season_duration(summary['average_duration_seconds'])}\n"
                f"**Shortest:** "
                f"{format_season_duration(summary['shortest_duration_seconds'])}"
                f"{shortest_match}\n"
                f"**Longest:** "
                f"{format_season_duration(summary['longest_duration_seconds'])}"
                f"{longest_match}\n"
                f"**Total play time:** "
                f"{format_season_duration(summary['total_duration_seconds'])}"
            ),
            inline=False,
        )

        await interaction.response.send_message(embed=embed)

    @app_commands.command(
        name="stats",
        description="Shows detailed current-season outcome and card statistics.",
    )
    async def stats(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        season = await get_active_season(interaction.guild_id)
        if season is None:
            await interaction.response.send_message(
                "No community season is currently active.", ephemeral=True
            )
            return

        summary = season_overview_summary(
            await season_rows(int(season["id"]))
        )
        embed = discord.Embed(
            title=f"{season_display_name(season)} — Detailed Statistics",
            description=f"Based on **{summary['matches']:,}** archived matches.",
            color=discord.Color.blurple(),
        )

        youtube_rate = (
            summary["youtube_matches"] / summary["matches"] * 100
            if summary["matches"] else 0.0
        )
        average_missions = (
            f"{summary['average_missions']:.2f}"
            if summary["average_missions"] is not None else "N/A"
        )
        embed.add_field(
            name="Additional Season Data",
            value=(
                f"**Average missions:** {average_missions}\n"
                f"**Average assassination phase:** "
                f"{format_season_duration(summary['average_assassination_seconds'])}\n"
                f"**YouTube recordings:** {summary['youtube_matches']:,}/"
                f"{summary['matches']:,} ({youtube_rate:.1f}%)"
            ),
            inline=False,
        )

        m1_total = summary["m1_passes"] + summary["m1_fails"]
        embed.add_field(
            name="Mission 1 Outcomes",
            value=(
                f"*Passes:* {summary['m1_passes']:,} "
                f"({summary['m1_passes'] / m1_total * 100 if m1_total else 0:.1f}%)\n"
                f"↳ {format_outcome_split(summary['m1_outcomes']['Pass'])}\n"
                f"*Fails:* {summary['m1_fails']:,} "
                f"({summary['m1_fails'] / m1_total * 100 if m1_total else 0:.1f}%)\n"
                f"↳ {format_outcome_split(summary['m1_outcomes']['Fail'])}"
            ),
            inline=False,
        )

        hammer_lines = [
            f"*Resistance hammer:* "
            f"{format_outcome_split(summary['m1_hammer_outcomes']['Resistance'])}",
            f"*Spy hammer:* "
            f"{format_outcome_split(summary['m1_hammer_outcomes']['Spy'])}",
        ]
        if summary["m1_hammer"]["Unknown"]:
            hammer_lines.append(
                f"*Unknown hammer:* "
                f"{format_outcome_split(summary['m1_hammer_outcomes']['Unknown'])}"
            )
        hammer_coverage = sum(summary["m1_hammer"].values())
        hammer_lines.append(
            f"**Coverage:** {hammer_coverage:,}/{summary['matches']:,} matches"
        )
        embed.add_field(
            name="Mission 1 Hammer",
            value="\n".join(hammer_lines),
            inline=False,
        )

        start_lines = [
            f"*Starts with Resistance:* "
            f"{format_outcome_split(summary['card_start_outcomes']['Resistance'])}",
            f"*Starts with Spy:* "
            f"{format_outcome_split(summary['card_start_outcomes']['Spy'])}",
        ]
        if summary["card_starts"]["Unknown"]:
            start_lines.append(
                f"*Unknown starting holder:* "
                f"{format_outcome_split(summary['card_start_outcomes']['Unknown'])}"
            )
        embed.add_field(
            name="Ref of the Rain — Starting Holder",
            value="\n".join(start_lines),
            inline=False,
        )

        unused_lines = [
            f"*Resistance held, never used:* "
            f"{format_outcome_split(summary['card_unused_outcomes']['Resistance'])}",
            f"*Spy held, never used:* "
            f"{format_outcome_split(summary['card_unused_outcomes']['Spy'])}",
        ]
        if summary["card_unused_outcomes"]["Unknown"][0]:
            unused_lines.append(
                f"*Unknown holder, never used:* "
                f"{format_outcome_split(summary['card_unused_outcomes']['Unknown'])}"
            )
        embed.add_field(
            name="Ref of the Rain — Never Used",
            value="\n".join(unused_lines),
            inline=False,
        )

        transition_lines = []
        for label, (games, spy_wins) in summary["card_transitions"].items():
            resistance_wins = games - spy_wins
            transition_lines.append(
                f"*{label}:* "
                f"{format_outcome_split([games, resistance_wins, spy_wins])}"
            )
        embed.add_field(
            name="Ref of the Rain — First Handoff",
            value=(
                "\n".join(transition_lines)
                + f"\n**Handoff coverage:** "
                f"{sum(item[0] for item in summary['card_transitions'].values()):,}/"
                f"{summary['matches']:,} matches"
            ),
            inline=False,
        )

        stopped_lines = [
            f"*{label}:* {format_outcome_split(values)}"
            for label, values in summary["card_first_handoff_stops"].items()
        ]
        embed.add_field(
            name="Ref of the Rain — Handoff Then Stopped",
            value="\n".join(stopped_lines),
            inline=False,
        )

        embed.add_field(
            name="Ref of the Rain — Handoff Count Summary",
            value=(
                f"*Zero handoffs:* "
                f"{format_outcome_split(summary['card_exact_handoff_outcomes']['Zero'])}\n"
                f"*One handoff:* "
                f"{format_outcome_split(summary['card_exact_handoff_outcomes']['One'])}\n"
                f"*Two handoffs:* "
                f"{format_outcome_split(summary['card_exact_handoff_outcomes']['Two'])}"
            ),
            inline=False,
        )

        chain_lines = []
        for label, (games, spy_wins) in summary["card_three_chains"].items():
            if not games:
                continue
            chain_lines.append(
                f"*{label}:* "
                f"{format_outcome_split([games, games - spy_wins, spy_wins])}"
            )
        embed.add_field(
            name="Ref of the Rain — First Three Holders",
            value=(
                (
                    "\n".join(chain_lines) if chain_lines
                    else "No matches recorded a third holder."
                )
                + f"\n**Three-holder coverage:** "
                f"{sum(item[0] for item in summary['card_three_chains'].values()):,}/"
                f"{summary['matches']:,} matches"
            ),
            inline=False,
        )

        await interaction.response.send_message(embed=embed)

    @app_commands.command(
        name="backfill-advanced-stats",
        description="Recovers old Shot, Ref, and M1 hammer data from match posts.",
    )
    @app_commands.describe(
        channel=(
            "Match-history channel to scan. Defaults to the configured channel."
        )
    )
    @bot_admin_only()
    async def backfill_advanced_stats_command(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return

        target_channel = channel
        if target_channel is None:
            config = await get_match_config(interaction.guild_id)
            if config is None or not config["channel_id"]:
                await interaction.response.send_message(
                    "No match-history channel is configured. Select one explicitly.",
                    ephemeral=True,
                )
                return
            resolved = self.bot.get_channel(int(config["channel_id"]))
            if resolved is None:
                try:
                    resolved = await self.bot.fetch_channel(
                        int(config["channel_id"])
                    )
                except discord.DiscordException:
                    resolved = None
            if not isinstance(resolved, discord.TextChannel):
                await interaction.response.send_message(
                    "The configured match-history channel is unavailable.",
                    ephemeral=True,
                )
                return
            target_channel = resolved

        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            result = await backfill_advanced_stats(
                interaction.guild_id,
                target_channel,
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "I cannot read the complete message history in that channel.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as error:
            await interaction.followup.send(
                f"Discord interrupted the archive scan: `{error}`",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="Advanced Statistics Backfill Complete",
            description=(
                f"Scanned {target_channel.mention} from its oldest available "
                "message through today."
            ),
            color=discord.Color.green(),
        )
        embed.add_field(
            name="Archive Coverage",
            value=(
                f"**Database matches:** {result.database_matches:,}\n"
                f"**Messages scanned:** {result.messages_scanned:,}\n"
                f"**Match posts identified:** {result.matches_found:,}\n"
                f"**Unmatched historical posts:** {result.unmatched_messages:,}"
            ),
            inline=False,
        )
        embed.add_field(
            name="Recovered",
            value=(
                f"**Shot targets:** {result.shots_recovered:,}\n"
                f"**Ref histories:** {result.ref_histories_recovered:,}\n"
                f"**M1 hammers:** {result.hammers_recovered:,}\n"
                f"**Already stored/no recoverable field:** "
                f"{result.already_complete:,}"
            ),
            inline=False,
        )
        embed.set_footer(
            text=(
                "Safe to run again: existing values are preserved and only "
                "missing historical fields are filled."
            )
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

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
                    f"{season_display_name(season)}."
                )
            )
            return

        view = RegisteredPlayersView(
            players=players,
            season_name=season_display_name(season),
            requesting_user_id=interaction.user.id,
        )

        await interaction.response.send_message(
            embed=view.build_embed(),
            view=view,
        )

    @app_commands.command(
        name="unlinked-players",
        description="Shows active-season players without a linked Discord account.",
    )
    async def unlinked_players(
        self,
        interaction: discord.Interaction,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return

        season = await get_active_season(interaction.guild_id)
        if season is None:
            await interaction.response.send_message(
                "No community season is currently active.", ephemeral=True
            )
            return

        usernames = await get_unlinked_registered_usernames(
            interaction.guild_id, int(season["id"])
        )
        if not usernames:
            await interaction.response.send_message(
                f"Every player in {season_display_name(season)} is linked to a Discord account."
            )
            return

        lines = [
            f"`{index:>2}.` `{username}`"
            for index, username in enumerate(usernames, start=1)
        ]
        view = RosterStatusView(
            title=f"{season_display_name(season)} — Unlinked Players",
            lines=lines,
            footer_label=f"{len(usernames)} unlinked player(s)",
            requesting_user_id=interaction.user.id,
        )
        await interaction.response.send_message(embed=view.build_embed(), view=view)

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
                    f"{season_display_name(season)}."
                )
            )
            return

        view = SeasonVoidListView(
            players=voided_players,
            season_name=season_display_name(season),
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
                    f"{season_display_name(season)}."
                ),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            (
                f"`{clean_username}` has been voided from "
                f"{season_display_name(season)}.\n"
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
                    f"{season_display_name(season)}."
                ),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            (
                f"`{removed['proavalon_username']}` is no longer "
                f"voided from {season_display_name(season)}.\n"
                "The account can register again the next time "
                "`/stats player` is used for it and it meets "
                "the normal season requirements."
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="fetch",
        description="Backfills this server's active season from another server.",
    )
    @app_commands.describe(
        season_prefix=(
            "Season prefix to fetch. Defaults to this server's active prefix."
        ),
        source_server_id=(
            "Optional Discord server ID to use when multiple sources exist."
        ),
    )
    @bot_admin_only()
    async def fetch_season(
        self,
        interaction: discord.Interaction,
        season_prefix: str | None = None,
        source_server_id: str | None = None,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        parsed_source_id: int | None = None
        if source_server_id is not None:
            try:
                parsed_source_id = int(source_server_id.strip())
                if parsed_source_id <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                await interaction.response.send_message(
                    "`source_server_id` must be a valid Discord server ID.",
                    ephemeral=True,
                )
                return

        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            result = await fetch_season_history(
                guild_id=interaction.guild_id,
                required_prefix=season_prefix,
                source_guild_id=parsed_source_id,
            )
        except (RuntimeError, ValueError) as error:
            await interaction.followup.send(
                str(error),
                ephemeral=True,
            )
            return
        except Exception as error:
            await interaction.followup.send(
                (
                    "Season history could not be fetched because of an "
                    f"unexpected database error: `{type(error).__name__}`."
                ),
                ephemeral=True,
            )
            raise

        await interaction.followup.send(
            (
                f"**{result['prefix']} history fetched successfully.**\n"
                f"Matches copied: **{result['copied_matches']}**\n"
                f"Matches skipped as duplicates: "
                f"**{result['skipped_matches']}**\n"
                f"Players newly registered: **{result['players_added']}**\n"
                f"Destination now contains: "
                f"**{result['total_matches']} matches** and "
                f"**{result['total_players']} registered players**\n"
                f"Source server ID: `{result['source_guild_id']}`"
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="set",
        description="Starts a new 10-player community season.",
    )
    @app_commands.describe(
        prefix="Required account prefix, such as S4, r1, or ABC.",
        name="Required display name, such as Community Season 4.",
    )
    @bot_admin_only()
    async def set_season(
        self,
        interaction: discord.Interaction,
        prefix: str,
        name: str,
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
                name,
            )
        except (RuntimeError, ValueError) as error:
            await interaction.response.send_message(
                str(error),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            (
                f"{season_display_name(season)} "
                "is now active.\n"
                f"Only ProAvalon usernames beginning with "
                f"`{season['required_prefix']}` are eligible.\n"
                "Eligible accounts must have at least one "
                "10-player game."
            )
        )

    @app_commands.command(
        name="rename",
        description="Changes the display name of the active community season.",
    )
    @app_commands.describe(
        name="New required display name for the active season.",
    )
    @bot_admin_only()
    async def rename_season(
        self,
        interaction: discord.Interaction,
        name: str,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        try:
            season = await rename_active_season(
                interaction.guild_id,
                name,
            )
        except ValueError as error:
            await interaction.response.send_message(
                str(error),
                ephemeral=True,
            )
            return

        if season is None:
            await interaction.response.send_message(
                "No community season is currently active.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            (
                "Season display name changed to "
                f"**{season_display_name(season)}**."
            ),
            ephemeral=True,
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
            season_name=season_display_name(season),
            guild_id=interaction.guild_id,
        )

        await interaction.response.send_message(
            (
                f"Are you sure you want to end Community "
                f"{season_display_name(season)}?\n"
                "Searchable matches, teams/roles, results, durations, missions, "
                "season account links, and YouTube links will remain. Detailed votes, "
                "rating-change payloads, recording metadata, and obsolete void-list "
                "entries will be removed to save space."
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
