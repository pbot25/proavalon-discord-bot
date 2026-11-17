from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import app_commands
from discord.ext import commands

from commands.stats import (
    registered_username_autocomplete,
    resolve_registered_username,
    resolve_stats_target,
)
from database.activity import build_activity_stats
from database.insights import season_rows
from database.seasons import get_active_season, season_display_name
from database.settings import get_guild_timezone


DEFAULT_DAYS = 7
ACTIVITY_PAGE_SIZE = 10


def _parse_date(value: str | None, label: str) -> tuple[date | None, str | None]:
    if value is None or not value.strip():
        return None, None
    try:
        return date.fromisoformat(value.strip()), None
    except ValueError:
        return None, f"`{label}` must use `YYYY-MM-DD`, for example `2026-11-01`."


def resolve_activity_period(
    *,
    now: datetime,
    timezone_name: str,
    days: int | None,
    start_date: str | None,
    end_date: str | None,
) -> tuple[datetime | None, datetime | None, str | None, str | None]:
    if days is not None and start_date and start_date.strip():
        return None, None, None, "Choose either `days` or `start_date`, not both."
    start_day, error = _parse_date(start_date, "start_date")
    if error:
        return None, None, None, error
    end_day, error = _parse_date(end_date, "end_date")
    if error:
        return None, None, None, error
    try:
        local_zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        local_zone = timezone.utc
        timezone_name = "UTC"

    now_utc = now.astimezone(timezone.utc)
    if end_day is not None:
        end_at = datetime.combine(end_day + timedelta(days=1), time.min, local_zone)
        end_at = min(end_at.astimezone(timezone.utc), now_utc)
    else:
        end_at = now_utc

    if start_day is not None:
        start_at = datetime.combine(start_day, time.min, local_zone).astimezone(timezone.utc)
    else:
        selected_days = days if days is not None else DEFAULT_DAYS
        start_at = end_at - timedelta(days=selected_days)

    if start_at >= end_at:
        return None, None, None, "The activity start must be before the activity end."

    local_start = start_at.astimezone(local_zone)
    local_end = end_at.astimezone(local_zone)
    label = (
        f"{local_start.strftime('%b %d, %Y')} – "
        f"{local_end.strftime('%b %d, %Y')} • {timezone_name}"
    )
    return start_at, end_at, label, None


def _mmr_text(entry: dict) -> str:
    covered = int(entry["mmr_games"])
    games = int(entry["games"])
    if covered == 0:
        return "MMR Δ N/A"
    change = entry["mmr_change"]
    change_text = f"{change:+g}" if isinstance(change, float) else f"{change:+d}"
    if covered < games:
        return f"MMR Δ {change_text} ({covered}/{games} captured)"
    return f"MMR Δ {change_text}"


def _activity_table(entries: list[dict], *, start_rank: int) -> str:
    """Format activity rows like the primary fixed-width leaderboard."""
    if not entries:
        return "No activity was found."
    display_names = []
    username_width = min(
        24,
        max(len("Username"), max(len(entry["username"]) for entry in entries)),
    )
    for entry in entries:
        username = entry["username"]
        display_names.append(
            username
            if len(username) <= username_width
            else username[: username_width - 1] + "…"
        )

    ranks = [str(rank) for rank in range(start_rank, start_rank + len(entries))]
    games = [f"{entry['games']:,}" for entry in entries]
    win_rates = [f"{entry['win_rate']:.1f}%" for entry in entries]
    mmr_changes = [
        (
            "N/A"
            if int(entry["mmr_games"]) == 0
            else f"{entry['mmr_change']:+g}"
        )
        for entry in entries
    ]
    rank_width = max(len("Rank"), max(map(len, ranks)))
    games_width = max(len("GP"), max(map(len, games)))
    win_rate_width = max(len("WR"), max(map(len, win_rates)))
    mmr_width = max(len("MMR Δ"), max(map(len, mmr_changes)))
    lines = [
        (
            f"{'Rank':>{rank_width}}  {'Username':<{username_width}}   "
            f"{'GP':>{games_width}}  {'WR':>{win_rate_width}}  "
            f"{'MMR Δ':>{mmr_width}}"
        ),
        (
            f"{'-' * rank_width}  {'-' * username_width}   "
            f"{'-' * games_width}  {'-' * win_rate_width}  "
            f"{'-' * mmr_width}"
        ),
    ]
    for rank, username, games_text, win_rate, mmr_change in zip(
        ranks, display_names, games, win_rates, mmr_changes
    ):
        lines.append(
            f"{rank:>{rank_width}}  {username:<{username_width}}   "
            f"{games_text:>{games_width}}  {win_rate:>{win_rate_width}}  "
            f"{mmr_change:>{mmr_width}}"
        )
    return "```\n" + "\n".join(lines) + "\n```"


def sort_activity_entries(entries: list[dict], sort_by: str) -> list[dict]:
    """Return activity entries in the selected leaderboard order."""
    if sort_by == "win_rate":
        key = lambda item: (
            -item["win_rate"],
            -item["games"],
            -item["mmr_change"],
            item["username"].casefold(),
        )
    elif sort_by == "mmr_change":
        key = lambda item: (
            int(item["mmr_games"]) == 0,
            -item["mmr_change"],
            -item["games"],
            -item["win_rate"],
            item["username"].casefold(),
        )
    else:
        key = lambda item: (
            -item["games"],
            -item["win_rate"],
            -item["mmr_change"],
            item["username"].casefold(),
        )
    return sorted(entries, key=key)


class ActivityLeaderboardView(discord.ui.View):
    def __init__(
        self,
        *,
        entries: list[dict],
        season_name: str,
        period_label: str,
        sort_label: str,
        requesting_user_id: int,
    ) -> None:
        super().__init__(timeout=180)
        self.entries = entries
        self.season_name = season_name
        self.period_label = period_label
        self.sort_label = sort_label
        self.requesting_user_id = requesting_user_id
        self.page = 0
        self.total_pages = max(1, math.ceil(len(entries) / ACTIVITY_PAGE_SIZE))
        self._update_buttons()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requesting_user_id:
            return True
        await interaction.response.send_message(
            "Only the person who opened this leaderboard can change pages.",
            ephemeral=True,
        )
        return False

    def _update_buttons(self) -> None:
        self.previous_button.disabled = self.page == 0
        self.next_button.disabled = self.page >= self.total_pages - 1

    def build_embed(self) -> discord.Embed:
        start = self.page * ACTIVITY_PAGE_SIZE
        page_entries = self.entries[start:start + ACTIVITY_PAGE_SIZE]
        embed = discord.Embed(
            title=f"{self.season_name} — Activity Leaderboard",
            description=(
                f"*{self.period_label}*\n\n"
                + _activity_table(page_entries, start_rank=start + 1)
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(
            text=(
                f"Sorted by {self.sort_label} • Page "
                f"{self.page + 1}/{self.total_pages} • {len(self.entries)} active players"
            )
        )
        return embed

    @discord.ui.button(label="Previous", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(0, self.page - 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Next", emoji="▶️", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = min(self.total_pages - 1, self.page + 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True


class ActivityCommands(
    commands.GroupCog,
    group_name="activity",
    group_description="View player activity over a selected period.",
):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def _context(self, interaction, days, start_date, end_date):
        if interaction.guild_id is None:
            return None, None, None, None, "This command can only be used in a server."
        season = await get_active_season(interaction.guild_id)
        if season is None:
            return None, None, None, None, "No community season is currently active."
        timezone_name = await get_guild_timezone(interaction.guild_id) or "UTC"
        start_at, end_at, label, error = resolve_activity_period(
            now=datetime.now(timezone.utc),
            timezone_name=timezone_name,
            days=days,
            start_date=start_date,
            end_date=end_date,
        )
        if error:
            return None, None, None, None, error
        return season, start_at, end_at, label, None

    @app_commands.command(name="player", description="Shows one player's activity in a time period.")
    @app_commands.describe(
        username="Registered player. Leave blank to use your linked account.",
        discord_user="Linked Discord @ to look up.",
        days="Number of days to include. Defaults to 7.",
        start_date="Start date in YYYY-MM-DD. Use instead of days.",
        end_date="Optional inclusive end date in YYYY-MM-DD.",
    )
    @app_commands.autocomplete(username=registered_username_autocomplete)
    async def player(
        self,
        interaction: discord.Interaction,
        username: str | None = None,
        discord_user: discord.Member | None = None,
        days: app_commands.Range[int, 1, 3650] | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> None:
        season, start_at, end_at, label, error = await self._context(
            interaction, days, start_date, end_date
        )
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        requested, error = await resolve_stats_target(
            interaction,
            season,
            username=username,
            discord_user=discord_user,
        )
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        resolved = await resolve_registered_username(int(season["id"]), requested)
        if resolved is None:
            await interaction.response.send_message(
                f"`{requested}` is not registered for {season_display_name(season)}.",
                ephemeral=True,
            )
            return
        rows = await season_rows(int(season["id"]))
        entries = build_activity_stats(rows, start_at=start_at, end_at=end_at)
        entry = next(
            (item for item in entries if item["username"].casefold() == resolved.casefold()),
            None,
        )
        if entry is None:
            await interaction.response.send_message(
                f"`{resolved}` played no archived games during **{label}**.",
                ephemeral=True,
            )
            return
        embed = discord.Embed(
            title=f"{resolved} — Activity",
            description=(
                f"*{label}*\n\n"
                f"**Games:** {entry['games']:,}\n"
                f"**Record:** {entry['wins']:,}W–{entry['losses']:,}L\n"
                f"**Win rate:** {entry['win_rate']:.1f}%\n"
                f"**{_mmr_text(entry)}**"
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"{season_display_name(season)} • Archived matches only")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="leaderboard", description="Ranks all active players in a time period.")
    @app_commands.describe(
        days="Number of days to include. Defaults to 7.",
        start_date="Start date in YYYY-MM-DD. Use instead of days.",
        end_date="Optional inclusive end date in YYYY-MM-DD.",
        sort_by="Ranking metric. Defaults to games played.",
    )
    @app_commands.choices(
        sort_by=[
            app_commands.Choice(name="Games Played", value="games"),
            app_commands.Choice(name="Win Rate", value="win_rate"),
            app_commands.Choice(name="MMR Change", value="mmr_change"),
        ]
    )
    async def leaderboard(
        self,
        interaction: discord.Interaction,
        days: app_commands.Range[int, 1, 3650] | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        sort_by: app_commands.Choice[str] | None = None,
    ) -> None:
        season, start_at, end_at, label, error = await self._context(
            interaction, days, start_date, end_date
        )
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        rows = await season_rows(int(season["id"]))
        entries = build_activity_stats(rows, start_at=start_at, end_at=end_at)
        if not entries:
            await interaction.response.send_message(
                f"No archived games were found during **{label}**.", ephemeral=True
            )
            return
        sort_value = sort_by.value if sort_by is not None else "games"
        sort_labels = {
            "games": "games played, then win rate",
            "win_rate": "win rate, then games played",
            "mmr_change": "MMR change, then games played",
        }
        entries = sort_activity_entries(entries, sort_value)
        view = ActivityLeaderboardView(
            entries=entries,
            season_name=season_display_name(season),
            period_label=label,
            sort_label=sort_labels[sort_value],
            requesting_user_id=interaction.user.id,
        )
        await interaction.response.send_message(embed=view.build_embed(), view=view)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ActivityCommands(bot))
