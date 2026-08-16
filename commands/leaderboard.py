import asyncio
import math
import time
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import app_commands
from discord.ext import commands, tasks

from database.ratings import get_cached_rating
from database.seasons import (
    get_active_season,
    get_season_players,
)
from database.settings import (
    disable_daily_leaderboard,
    get_daily_leaderboard_configs,
    get_enabled_daily_leaderboards,
    get_guild_timezone,
    mark_daily_leaderboard_posted,
    set_daily_leaderboard_config,
    set_guild_timezone,
)
from proavalon.rating_tracker import ProAvalonRatingTracker
from proavalon.profile_scraper import (
    PlayerProfile,
    ProAvalonError,
    ProAvalonLoginError,
    ProAvalonUserNotFound,
)
from utils.permissions import bot_admin_only


PAGE_SIZE = 15
DEFAULT_MINIMUM_GAMES = 5

# Normal /leaderboard requests can only cause a ProAvalon refresh once
# every two hours per Discord server.
LEADERBOARD_CACHE_SECONDS = 2 * 60 * 60

# If the daily scheduled post happens right after another refresh, reuse
# that very recent refresh instead of scraping everyone twice.
DAILY_REFRESH_GRACE_SECONDS = 5 * 60


LEADERBOARD_CHOICES = [
    app_commands.Choice(
        name="Total Time Played",
        value="total_time",
    ),
    app_commands.Choice(
        name="Most Games Played",
        value="games_played",
    ),
    app_commands.Choice(name="Rating", value="rating"),
    app_commands.Choice(name="Overall WR", value="overall"),
    app_commands.Choice(name="Resistance WR", value="resistance"),
    app_commands.Choice(name="Spy WR", value="spy"),
    app_commands.Choice(name="VT", value="vt"),
    app_commands.Choice(name="Lover", value="lover"),
    app_commands.Choice(name="Percival", value="percival"),
    app_commands.Choice(name="Merlin", value="merlin"),
    app_commands.Choice(name="Assassin", value="assassin"),
    app_commands.Choice(name="Morgana", value="morgana"),
    app_commands.Choice(name="Mordred", value="mordred"),
    app_commands.Choice(name="Oberon", value="oberon"),
]

# Every statistic supported by /leaderboard is also available for
# automatic daily leaderboards.
DAILY_LEADERBOARD_CHOICES = list(LEADERBOARD_CHOICES)

DISPLAY_NAMES = {
    "total_time": "Total Time Played",
    "games_played": "Most Games Played",
    "rating": "Rating",
    "overall": "Overall Win Rate",
    "resistance": "Resistance Win Rate",
    "spy": "Spy Win Rate",
    "vt": "VT Win Rate",
    "lover": "Lover Win Rate",
    "percival": "Percival Win Rate",
    "merlin": "Merlin Win Rate",
    "assassin": "Assassin Win Rate",
    "morgana": "Morgana Win Rate",
    "mordred": "Mordred Win Rate",
    "oberon": "Oberon Win Rate",
}

RATING_BRACKET_EMOJIS = {
    "unranked": "⁉️",
    "iron": "⚙️",
    "bronze": "🥉",
    "silver": "🥈",
    "gold": "🥇",
    "platinum": "🔷",
    "diamond": "💎",
    "champion": "🏆",
}

RATING_BRACKET_SCORES = {
    "iron": 0,
    "bronze": 1,
    "silver": 2,
    "gold": 3,
    "platinum": 4,
    "diamond": 5,
    "champion": 6,
}

COMMON_TIMEZONES = [
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "America/Phoenix",
    "America/Anchorage",
    "Pacific/Honolulu",
    "America/Toronto",
    "Europe/London",
    "Europe/Paris",
    "Asia/Kolkata",
    "Asia/Tokyo",
    "Australia/Sydney",
    "UTC",
]


@dataclass
class LeaderboardEntry:
    username: str
    games: int
    wins: int
    losses: int
    rating_bracket: str | None = None
    rating: int | None = None
    rating_updated_at: str | None = None
    total_time_seconds: float | None = None

    @property
    def win_rate(self) -> float:
        if self.games == 0:
            return 0.0

        return self.wins / self.games * 100

    @property
    def rating_score(self) -> int:
        if self.rating_bracket is None:
            return -1

        return RATING_BRACKET_SCORES.get(
            self.rating_bracket.casefold(),
            -1,
        )


def format_play_time(
    seconds: float | None,
) -> str:
    """Format elapsed play time for leaderboard display."""

    if seconds is None:
        return "N/A"

    total_minutes = max(
        0,
        int(seconds // 60),
    )

    hours, minutes = divmod(
        total_minutes,
        60,
    )

    if hours == 0:
        return f"{minutes}m"

    if minutes == 0:
        return f"{hours:,}h"

    return f"{hours:,}h {minutes}m"


def get_rating_bracket_emoji(
    rating_bracket: str | None,
) -> str:
    if rating_bracket is None:
        return ""

    return RATING_BRACKET_EMOJIS.get(
        rating_bracket.casefold(),
        "",
    )


def get_role_alignment(
    profile: PlayerProfile,
    role_name: str,
) -> str | None:
    target = role_name.casefold()

    for stored_role, alignment in profile.role_alliances.items():
        if stored_role.casefold() == target:
            return alignment

    return None


def get_metric_record(
    profile: PlayerProfile,
    game_size_key: str,
    metric: str,
) -> tuple[int, int]:
    """
    Return wins/losses for a leaderboard statistic.

    Rating uses the player's overall tracked-game record for GP/WR while
    sorting by the public ProAvalon rating bracket.
    """

    roles = profile.role_stats.get(game_size_key, {})

    wins = 0
    losses = 0

    if metric == "total_time":
        # Total time played is a global account statistic rather than a
        # game-size-specific statistic. Use the global W/L record so the
        # entry also carries the account's total GP.
        return (
            profile.total_wins,
            profile.total_losses,
        )

    if metric in {"overall", "rating", "games_played"}:
        for record in roles.values():
            wins += record.wins
            losses += record.losses

        return wins, losses

    if metric in {"resistance", "spy"}:
        for role_name, record in roles.items():
            alignment = get_role_alignment(
                profile,
                role_name,
            )

            if (
                alignment is not None
                and alignment.casefold() == metric
            ):
                wins += record.wins
                losses += record.losses

        return wins, losses

    for role_name, record in roles.items():
        normalized_role = role_name.casefold()

        if metric == "vt":
            matches = normalized_role == "resistance"
        elif metric == "lover":
            matches = normalized_role in {
                "isolde",
                "tristan",
            }
        else:
            matches = normalized_role == metric

        if matches:
            wins += record.wins
            losses += record.losses

    return wins, losses


def build_leaderboard_entry(
    profile: PlayerProfile,
    game_size_key: str,
    metric: str,
) -> LeaderboardEntry:
    wins, losses = get_metric_record(
        profile=profile,
        game_size_key=game_size_key,
        metric=metric,
    )

    rating_snapshot = get_cached_rating(
        profile.username
    )

    return LeaderboardEntry(
        username=profile.username,
        games=wins + losses,
        wins=wins,
        losses=losses,
        rating_bracket=(
            rating_snapshot.rating_bracket
            if rating_snapshot is not None
            and rating_snapshot.rating_bracket is not None
            else profile.rating_bracket
        ),
        rating=(
            rating_snapshot.rating
            if rating_snapshot is not None
            else None
        ),
        rating_updated_at=(
            rating_snapshot.rating_updated_at
            if rating_snapshot is not None
            else None
        ),
        total_time_seconds=(
            profile.total_time_played_seconds
        ),
    )


def sort_entries(
    entries: list[LeaderboardEntry],
    metric: str,
) -> list[LeaderboardEntry]:
    if metric == "total_time":
        return sorted(
            entries,
            key=lambda entry: (
                -(
                    entry.total_time_seconds
                    if entry.total_time_seconds is not None
                    else -1
                ),
                -entry.games,
                entry.username.casefold(),
            ),
        )

    if metric == "rating":
        return sorted(
            entries,
            key=lambda entry: (
                -(
                    entry.rating
                    if entry.rating is not None
                    else -1
                ),
                -entry.win_rate,
                -entry.games,
                entry.username.casefold(),
            ),
        )

    if metric == "games_played":
        return sorted(
            entries,
            key=lambda entry: (
                -entry.games,
                -entry.win_rate,
                entry.username.casefold(),
            ),
        )

    return sorted(
        entries,
        key=lambda entry: (
            -entry.win_rate,
            -entry.games,
            entry.username.casefold(),
        ),
    )


def leaderboard_shows_tier(metric: str) -> bool:
    return metric in {"rating", "overall"}


def get_display_tier(
    entry: LeaderboardEntry,
    metric: str,
) -> str:
    """
    Return one symbol for every row on Rating/Overall leaderboards.

    Keeping the symbol in its own column prevents Discord's variable-width
    emoji rendering from shifting the username and separator columns.
    """
    if not leaderboard_shows_tier(metric):
        return ""

    emoji = get_rating_bracket_emoji(
        entry.rating_bracket
    )

    # If ProAvalon has not supplied a bracket yet, still reserve the same
    # visual slot instead of allowing this row to shift left.
    return emoji or "⁉️"


def format_tier_column(
    entry: LeaderboardEntry,
    metric: str,
) -> str:
    """
    Normalize the visual width of Discord tier emojis.

    Emoji strings do not have reliable monospace widths in Discord. The
    unranked interrobang contains a variation selector, while medal/rank
    emojis do not, so use explicit spacing rather than Python field-width
    padding.
    """
    tier = get_display_tier(entry, metric)

    # Discord renders the medal/rank emoji one monospace cell wider
    # than the interrobang in code blocks. Use one fewer literal space
    # after ranked emoji so every username begins in the same column.
    if tier == "⁉️":
        return f"{tier}   "

    return f"{tier}   "


def get_display_username(
    entry: LeaderboardEntry,
    metric: str,
) -> str:
    return entry.username


def build_page_description(
    entries: list[LeaderboardEntry],
    page: int,
    metric: str,
) -> str:
    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    page_entries = entries[start:end]

    if not page_entries:
        return "No qualifying players."

    rank_width = max(
        len("Rank"),
        len(str(len(entries))),
    )

    display_names = [
        get_display_username(entry, metric)
        for entry in page_entries
    ]

    username_width = max(
        len("Username"),
        max(len(name) for name in display_names),
    )

    games_texts = [
        f"{entry.games:,}"
        for entry in page_entries
    ]
    games_width = max(
        len("GP"),
        max(len(text) for text in games_texts),
    )

    show_tier = leaderboard_shows_tier(metric)

    if metric == "total_time":
        metric_header = "Time"
        metric_texts = [
            format_play_time(entry.total_time_seconds)
            for entry in page_entries
        ]
    elif metric == "rating":
        metric_header = "Rating"
        metric_texts = [
            (
                f"{entry.rating:,}"
                if entry.rating is not None
                else "N/A"
            )
            for entry in page_entries
        ]
    else:
        metric_header = "WR"
        metric_texts = [
            f"{entry.win_rate:.1f}%"
            for entry in page_entries
        ]

    metric_width = max(
        len(metric_header),
        max(len(value) for value in metric_texts),
    )

    # IMPORTANT:
    # Discord emoji do not obey monospace code-block character widths.
    # Therefore Tier is intentionally the LAST column. This guarantees
    # Rank, Username, GP, and WR/Rating stay perfectly aligned regardless
    # of which emoji Discord renders for the tier.
    if show_tier:
        header = (
            f"{'Rank':>{rank_width}}  "
            f"{'Username':<{username_width}}   "
            f"{'GP':>{games_width}}  "
            f"{metric_header:>{metric_width}}  "
            f"Tier"
        )
        separator = (
            f"{'-' * rank_width}  "
            f"{'-' * username_width}   "
            f"{'-' * games_width}  "
            f"{'-' * metric_width}  "
            f"----"
        )
    else:
        header = (
            f"{'Rank':>{rank_width}}  "
            f"{'Username':<{username_width}}   "
            f"{'GP':>{games_width}}  "
            f"{metric_header:>{metric_width}}"
        )
        separator = (
            f"{'-' * rank_width}  "
            f"{'-' * username_width}   "
            f"{'-' * games_width}  "
            f"{'-' * metric_width}"
        )

    lines = [header, separator]

    for rank, (
        entry,
        display_name,
        metric_text,
    ) in enumerate(
        zip(
            page_entries,
            display_names,
            metric_texts,
        ),
        start=start + 1,
    ):
        games_text = f"{entry.games:,}"

        if show_tier:
            tier = get_display_tier(entry, metric)
            lines.append(
                f"{rank:>{rank_width}}  "
                f"{display_name:<{username_width}}   "
                f"{games_text:>{games_width}}  "
                f"{metric_text:>{metric_width}}  "
                f"{tier}"
            )
        else:
            lines.append(
                f"{rank:>{rank_width}}  "
                f"{display_name:<{username_width}}   "
                f"{games_text:>{games_width}}  "
                f"{metric_text:>{metric_width}}"
            )

    return (
        "```\n"
        + "\n".join(lines)
        + "\n```"
    )


DAILY_DESCRIPTION_LIMIT = 4096


def build_daily_table(
    entries: list[LeaderboardEntry],
    metric: str,
) -> tuple[str, int]:
    """
    Build the automatic daily table using the same layout as /leaderboard.

    Tier is kept as the final column because Discord emoji use variable
    visual widths even inside code blocks. Keeping emoji last means the
    numeric/text columns before it remain perfectly aligned.
    """

    if not entries:
        return "No qualifying players.", 0

    rank_width = max(
        len("Rank"),
        len(str(len(entries))),
    )

    display_names = [
        get_display_username(entry, metric)
        for entry in entries
    ]

    username_width = max(
        len("Username"),
        min(
            32,
            max(len(name) for name in display_names),
        ),
    )

    games_texts = [
        f"{entry.games:,}"
        for entry in entries
    ]
    games_width = max(
        len("GP"),
        max(len(value) for value in games_texts),
    )

    if metric == "total_time":
        metric_header = "Time"
        metric_texts = [
            format_play_time(entry.total_time_seconds)
            for entry in entries
        ]
    elif metric == "rating":
        metric_header = "Rating"
        metric_texts = [
            (
                f"{entry.rating:,}"
                if entry.rating is not None
                else "N/A"
            )
            for entry in entries
        ]
    else:
        metric_header = "WR"
        metric_texts = [
            f"{entry.win_rate:.1f}%"
            for entry in entries
        ]

    metric_width = max(
        len(metric_header),
        max(len(value) for value in metric_texts),
    )

    show_tier = leaderboard_shows_tier(metric)

    if show_tier:
        header = (
            f"{'Rank':>{rank_width}}  "
            f"{'Username':<{username_width}}   "
            f"{'GP':>{games_width}}  "
            f"{metric_header:>{metric_width}}  "
            f"Tier"
        )
        separator = (
            f"{'-' * rank_width}  "
            f"{'-' * username_width}   "
            f"{'-' * games_width}  "
            f"{'-' * metric_width}  "
            f"----"
        )
    else:
        header = (
            f"{'Rank':>{rank_width}}  "
            f"{'Username':<{username_width}}   "
            f"{'GP':>{games_width}}  "
            f"{metric_header:>{metric_width}}"
        )
        separator = (
            f"{'-' * rank_width}  "
            f"{'-' * username_width}   "
            f"{'-' * games_width}  "
            f"{'-' * metric_width}"
        )

    lines = [header, separator]
    displayed = 0

    for rank, (
        entry,
        display_name,
        metric_text,
    ) in enumerate(
        zip(
            entries,
            display_names,
            metric_texts,
        ),
        start=1,
    ):
        if len(display_name) > username_width:
            display_name = (
                display_name[: username_width - 1]
                + "…"
            )

        if show_tier:
            tier = get_display_tier(entry, metric)
            row = (
                f"{rank:>{rank_width}}  "
                f"{display_name:<{username_width}}   "
                f"{entry.games:>{games_width},}  "
                f"{metric_text:>{metric_width}}  "
                f"{tier}"
            )
        else:
            row = (
                f"{rank:>{rank_width}}  "
                f"{display_name:<{username_width}}   "
                f"{entry.games:>{games_width},}  "
                f"{metric_text:>{metric_width}}"
            )

        candidate = (
            "```\n"
            + "\n".join(lines + [row])
            + "\n```"
        )

        if len(candidate) > DAILY_DESCRIPTION_LIMIT:
            break

        lines.append(row)
        displayed += 1

    if displayed == 0:
        return "No qualifying players fit in the embed.", 0

    return (
        "```\n"
        + "\n".join(lines)
        + "\n```",
        displayed,
    )


def build_daily_embed(
    *,
    season_number: int,
    entries: list[LeaderboardEntry],
    metric: str,
    minimum_games: int,
    timezone_name: str,
    post_time: str,
) -> discord.Embed:
    display_name = DISPLAY_NAMES[metric]

    description, displayed = build_daily_table(
        entries=entries,
        metric=metric,
    )

    embed = discord.Embed(
        title=(
            f"Community Season {season_number} — "
            f"Daily {display_name}"
        ),
        description=description,
        color=discord.Color.blurple(),
    )

    embed.set_footer(
        text=(
            f"Showing {displayed} of "
            f"{len(entries)} qualifying players • "
            f"Minimum games: {minimum_games} • "
            f"Daily at {post_time} {timezone_name}"
        )
    )

    return embed


class LeaderboardView(discord.ui.View):
    def __init__(
        self,
        *,
        entries: list[LeaderboardEntry],
        season_number: int,
        metric: str,
        minimum_games: int,
        requested_limit: int | None,
        total_qualifying_before_limit: int,
        refreshed_count: int,
        failed_count: int,
        cache_hit: bool,
        cache_age_seconds: int | None,
        requesting_user_id: int,
    ) -> None:
        super().__init__(timeout=180)

        self.entries = entries
        self.season_number = season_number
        self.metric = metric
        self.minimum_games = minimum_games
        self.requested_limit = requested_limit
        self.total_qualifying_before_limit = (
            total_qualifying_before_limit
        )
        self.refreshed_count = refreshed_count
        self.failed_count = failed_count
        self.cache_hit = cache_hit
        self.cache_age_seconds = cache_age_seconds
        self.requesting_user_id = requesting_user_id

        self.page = 0
        self.total_pages = max(
            1,
            math.ceil(len(entries) / PAGE_SIZE),
        )

        self._update_buttons()

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id != self.requesting_user_id:
            await interaction.response.send_message(
                (
                    "Only the person who opened this leaderboard "
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
        display_name = DISPLAY_NAMES[self.metric]

        if self.entries:
            start = self.page * PAGE_SIZE + 1
            end = min(
                (self.page + 1) * PAGE_SIZE,
                len(self.entries),
            )
            showing_text = (
                f"Showing {start}–{end} of "
                f"{len(self.entries)} displayed players"
            )
        else:
            showing_text = "No qualifying players"

        embed = discord.Embed(
            title=(
                f"Community Season {self.season_number} — "
                f"{display_name}"
            ),
            description=build_page_description(
                entries=self.entries,
                page=self.page,
                metric=self.metric,
            ),
            color=discord.Color.blurple(),
        )

        footer_parts = [
            showing_text,
            f"Page {self.page + 1}/{self.total_pages}",
            f"Minimum games: {self.minimum_games}",
        ]

        if self.requested_limit is not None:
            footer_parts.append(
                f"Top: {self.requested_limit}"
            )

        footer_parts.append(
            (
                "Qualifying before limit: "
                f"{self.total_qualifying_before_limit}"
            )
        )

        if self.cache_hit:
            cache_age = (
                self.cache_age_seconds
                if self.cache_age_seconds is not None
                else 0
            )
            footer_parts.append(
                (
                    f"Cached: {cache_age // 60}m "
                    f"{cache_age % 60}s old"
                )
            )
        else:
            footer_parts.append(
                f"Freshly refreshed: {self.refreshed_count}"
            )

        if self.failed_count:
            footer_parts.append(
                f"Failed: {self.failed_count}"
            )

        embed.set_footer(
            text=" • ".join(footer_parts)
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


class LeaderboardCommands(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
    ) -> None:
        self.bot = bot

        # Every Discord server receives a completely independent cache.
        self._cached_profiles: dict[
            int,
            dict[str, PlayerProfile],
        ] = {}
        self._cache_refreshed_at: dict[int, float] = {}
        self._cache_season_id: dict[int, int] = {}
        self._cache_registered_usernames: dict[
            int,
            set[str],
        ] = {}
        self._cache_failed_count: dict[int, int] = {}
        self._refresh_locks: dict[int, asyncio.Lock] = {}

        self.rating_tracker = ProAvalonRatingTracker(
            bot.proavalon_scraper,
            bot=bot,
        )
        self._rating_tracker_start_task = (
            asyncio.create_task(
                self._start_rating_tracker_when_ready()
            )
        )

    async def _start_rating_tracker_when_ready(
        self,
    ) -> None:
        await self.bot.wait_until_ready()
        self.rating_tracker.start()

    def cog_unload(self) -> None:
        if not self._rating_tracker_start_task.done():
            self._rating_tracker_start_task.cancel()

        asyncio.create_task(
            self.rating_tracker.stop()
        )

    def _get_refresh_lock(
        self,
        guild_id: int,
    ) -> asyncio.Lock:
        lock = self._refresh_locks.get(guild_id)

        if lock is None:
            lock = asyncio.Lock()
            self._refresh_locks[guild_id] = lock

        return lock

    def _cache_age_seconds(
        self,
        guild_id: int,
    ) -> int | None:
        refreshed_at = self._cache_refreshed_at.get(
            guild_id
        )

        if refreshed_at is None:
            return None

        return max(
            0,
            int(time.monotonic() - refreshed_at),
        )

    def _cache_matches_roster(
        self,
        *,
        guild_id: int,
        season_id: int,
        registered_usernames: set[str],
    ) -> bool:
        return (
            self._cache_season_id.get(guild_id)
            == season_id
            and self._cache_registered_usernames.get(
                guild_id
            )
            == registered_usernames
            and guild_id in self._cached_profiles
        )

    def _cache_is_valid(
        self,
        *,
        guild_id: int,
        season_id: int,
        registered_usernames: set[str],
        max_age_seconds: int,
    ) -> bool:
        if not self._cache_matches_roster(
            guild_id=guild_id,
            season_id=season_id,
            registered_usernames=registered_usernames,
        ):
            return False

        age = self._cache_age_seconds(guild_id)

        if age is None:
            return False

        return age < max_age_seconds

    async def _scrape_registered_profiles(
        self,
        *,
        guild_id: int,
        season_id: int,
        players,
    ) -> tuple[list[PlayerProfile], int, int]:
        registered_usernames = {
            player["proavalon_username"].casefold()
            for player in players
        }

        profiles_by_username: dict[
            str,
            PlayerProfile,
        ] = {}
        failed_count = 0

        for player in players:
            username = player["proavalon_username"]

            try:
                profile = (
                    await self.bot.proavalon_scraper
                    .get_profile_data(username)
                )

            except (
                ProAvalonUserNotFound,
                ProAvalonLoginError,
                ProAvalonError,
                asyncio.TimeoutError,
                ValueError,
            ):
                failed_count += 1
                continue

            profiles_by_username[
                profile.username.casefold()
            ] = profile

        self._cached_profiles[guild_id] = (
            profiles_by_username
        )
        self._cache_refreshed_at[guild_id] = (
            time.monotonic()
        )
        self._cache_season_id[guild_id] = season_id
        self._cache_registered_usernames[guild_id] = (
            registered_usernames
        )
        self._cache_failed_count[guild_id] = (
            failed_count
        )

        return (
            list(profiles_by_username.values()),
            len(profiles_by_username),
            failed_count,
        )

    async def get_leaderboard_profiles(
        self,
        *,
        guild_id: int,
        season_id: int,
        players,
        force_refresh: bool = False,
        allow_recent_force_reuse: bool = False,
    ) -> tuple[list[PlayerProfile], int, int, bool]:
        """
        Return the shared guild leaderboard snapshot.

        Normal commands refresh only when the snapshot is at least two
        hours old. The per-guild lock is rechecked after acquisition, so
        simultaneous commands cannot trigger back-to-back scrapes.
        """

        registered_usernames = {
            player["proavalon_username"].casefold()
            for player in players
        }

        if not force_refresh:
            if self._cache_is_valid(
                guild_id=guild_id,
                season_id=season_id,
                registered_usernames=registered_usernames,
                max_age_seconds=LEADERBOARD_CACHE_SECONDS,
            ):
                return (
                    list(
                        self._cached_profiles[
                            guild_id
                        ].values()
                    ),
                    0,
                    self._cache_failed_count.get(
                        guild_id,
                        0,
                    ),
                    True,
                )

        elif allow_recent_force_reuse:
            if self._cache_is_valid(
                guild_id=guild_id,
                season_id=season_id,
                registered_usernames=registered_usernames,
                max_age_seconds=DAILY_REFRESH_GRACE_SECONDS,
            ):
                return (
                    list(
                        self._cached_profiles[
                            guild_id
                        ].values()
                    ),
                    0,
                    self._cache_failed_count.get(
                        guild_id,
                        0,
                    ),
                    True,
                )

        async with self._get_refresh_lock(guild_id):
            # Recheck after waiting. Another request may already have
            # completed the refresh while we were queued.
            if not force_refresh:
                if self._cache_is_valid(
                    guild_id=guild_id,
                    season_id=season_id,
                    registered_usernames=registered_usernames,
                    max_age_seconds=LEADERBOARD_CACHE_SECONDS,
                ):
                    return (
                        list(
                            self._cached_profiles[
                                guild_id
                            ].values()
                        ),
                        0,
                        self._cache_failed_count.get(
                            guild_id,
                            0,
                        ),
                        True,
                    )

            elif allow_recent_force_reuse:
                if self._cache_is_valid(
                    guild_id=guild_id,
                    season_id=season_id,
                    registered_usernames=registered_usernames,
                    max_age_seconds=DAILY_REFRESH_GRACE_SECONDS,
                ):
                    return (
                        list(
                            self._cached_profiles[
                                guild_id
                            ].values()
                        ),
                        0,
                        self._cache_failed_count.get(
                            guild_id,
                            0,
                        ),
                        True,
                    )

            profiles, refreshed, failed = (
                await self._scrape_registered_profiles(
                    guild_id=guild_id,
                    season_id=season_id,
                    players=players,
                )
            )

            return (
                profiles,
                refreshed,
                failed,
                False,
            )

    def build_entries(
        self,
        *,
        profiles: list[PlayerProfile],
        tracked_player_count: int,
        metric: str,
        minimum_games: int,
    ) -> list[LeaderboardEntry]:
        game_size_key = f"{tracked_player_count}p"

        entries = [
            build_leaderboard_entry(
                profile=profile,
                game_size_key=game_size_key,
                metric=metric,
            )
            for profile in profiles
        ]

        entries = [
            entry
            for entry in entries
            if entry.games >= minimum_games
        ]

        return sort_entries(
            entries,
            metric=metric,
        )

    @app_commands.command(
        name="leaderboard",
        description="Shows this server's active-season leaderboard.",
    )
    @app_commands.describe(
        statistic="Role, alignment, overall, or rating leaderboard.",
        players_shown=(
            "Optional maximum number of players to display."
        ),
        minimum_games=(
            "Optional minimum games required to qualify. "
            f"Defaults to {DEFAULT_MINIMUM_GAMES}."
        ),
    )
    @app_commands.choices(
        statistic=LEADERBOARD_CHOICES,
    )
    async def leaderboard(
        self,
        interaction: discord.Interaction,
        statistic: app_commands.Choice[str],
        players_shown: app_commands.Range[int, 1, 500] | None = None,
        minimum_games: app_commands.Range[int, 0, 999]
        = DEFAULT_MINIMUM_GAMES,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        guild_id = interaction.guild_id
        season = await get_active_season(guild_id)

        if season is None:
            await interaction.response.send_message(
                "No community season is currently active in this server.",
                ephemeral=True,
            )
            return

        players = await get_season_players(
            season["id"]
        )

        if not players:
            await interaction.response.send_message(
                (
                    f"No players are registered for "
                    f"Community Season {season['season_number']} "
                    "in this server."
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer(
            thinking=True,
        )

        (
            profiles,
            refreshed_count,
            failed_count,
            cache_hit,
        ) = await self.get_leaderboard_profiles(
            guild_id=guild_id,
            season_id=int(season["id"]),
            players=players,
        )

        entries = self.build_entries(
            profiles=profiles,
            tracked_player_count=int(
                season["tracked_player_count"]
            ),
            metric=statistic.value,
            minimum_games=minimum_games,
        )

        total_qualifying_before_limit = len(entries)

        if players_shown is None:
            displayed_entries = entries
        else:
            displayed_entries = entries[
                :players_shown
            ]

        view = LeaderboardView(
            entries=displayed_entries,
            season_number=int(
                season["season_number"]
            ),
            metric=statistic.value,
            minimum_games=minimum_games,
            requested_limit=players_shown,
            total_qualifying_before_limit=(
                total_qualifying_before_limit
            ),
            refreshed_count=refreshed_count,
            failed_count=failed_count,
            cache_hit=cache_hit,
            cache_age_seconds=self._cache_age_seconds(
                guild_id
            ),
            requesting_user_id=interaction.user.id,
        )

        await interaction.followup.send(
            embed=view.build_embed(),
            view=view,
        )

    @app_commands.command(
        name="leaderboard-refresh",
        description="Force-refreshes this server's leaderboard cache.",
    )
    @bot_admin_only()
    async def leaderboard_refresh(
        self,
        interaction: discord.Interaction,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        guild_id = interaction.guild_id
        season = await get_active_season(guild_id)

        if season is None:
            await interaction.response.send_message(
                "No community season is currently active in this server.",
                ephemeral=True,
            )
            return

        players = await get_season_players(
            season["id"]
        )

        if not players:
            await interaction.response.send_message(
                "No registered players are available to refresh.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(
            thinking=True,
            ephemeral=True,
        )

        (
            profiles,
            refreshed_count,
            failed_count,
            _,
        ) = await self.get_leaderboard_profiles(
            guild_id=guild_id,
            season_id=int(season["id"]),
            players=players,
            force_refresh=True,
            allow_recent_force_reuse=False,
        )

        await interaction.followup.send(
            (
                f"Leaderboard cache refreshed for "
                f"Community Season {season['season_number']}.\n"
                f"Successfully refreshed: {refreshed_count}\n"
                f"Failed: {failed_count}\n"
                f"Cached profiles: {len(profiles)}\n"
                "Normal leaderboard requests will reuse this snapshot "
                "for up to 2 hours."
            ),
            ephemeral=True,
        )

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.CheckFailure):
            message = (
                "You do not have bot administrator permission "
                "for this server."
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


async def timezone_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    normalized = current.strip().casefold()

    matches = [
        timezone_name
        for timezone_name in COMMON_TIMEZONES
        if normalized in timezone_name.casefold()
    ]

    return [
        app_commands.Choice(
            name=timezone_name,
            value=timezone_name,
        )
        for timezone_name in matches[:25]
    ]


class ServerConfigCommands(
    commands.GroupCog,
    group_name="server",
    group_description="Configure this server's ProAvalon bot settings.",
):
    def __init__(
        self,
        bot: commands.Bot,
    ) -> None:
        self.bot = bot

    @app_commands.command(
        name="timezone",
        description="Sets or displays this server's timezone.",
    )
    @app_commands.describe(
        timezone=(
            "IANA timezone, e.g. America/New_York. "
            "Leave blank to view the current timezone."
        ),
    )
    @app_commands.autocomplete(
        timezone=timezone_autocomplete,
    )
    @bot_admin_only()
    async def timezone(
        self,
        interaction: discord.Interaction,
        timezone: str | None = None,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        if timezone is None:
            current = await get_guild_timezone(
                interaction.guild_id
            )

            if current is None:
                await interaction.response.send_message(
                    (
                        "No timezone is configured for this server.\n"
                        "Example: `/server timezone "
                        "timezone:America/New_York`"
                    ),
                    ephemeral=True,
                )
                return

            await interaction.response.send_message(
                f"Server timezone: `{current}`",
                ephemeral=True,
            )
            return

        clean_timezone = timezone.strip()

        try:
            ZoneInfo(clean_timezone)
        except ZoneInfoNotFoundError:
            await interaction.response.send_message(
                (
                    f"`{clean_timezone}` is not a valid installed "
                    "IANA timezone.\n"
                    "Example: `America/New_York`"
                ),
                ephemeral=True,
            )
            return

        await set_guild_timezone(
            interaction.guild_id,
            clean_timezone,
        )

        await interaction.response.send_message(
            (
                f"Server timezone set to "
                f"`{clean_timezone}`."
            ),
            ephemeral=True,
        )


class DailyLeaderboardCommands(
    commands.GroupCog,
    group_name="daily-leaderboard",
    group_description="Configure the automatic daily leaderboard.",
):
    def __init__(
        self,
        bot: commands.Bot,
    ) -> None:
        self.bot = bot
        self._failed_daily_attempts: set[
            tuple[int, str]
        ] = set()
        self.daily_post_loop.start()

    def cog_unload(self) -> None:
        self.daily_post_loop.cancel()

    def _leaderboard_cog(
        self,
    ) -> LeaderboardCommands | None:
        cog = self.bot.get_cog(
            "LeaderboardCommands"
        )

        if isinstance(cog, LeaderboardCommands):
            return cog

        return None

    @app_commands.command(
        name="set",
        description="Adds or updates a daily leaderboard for a channel.",
    )
    @app_commands.describe(
        leaderboard_type="Leaderboard to post each day.",
        channel="Channel where the leaderboard will be posted.",
        time="Local 24-hour time in HH:MM format, e.g. 20:00.",
        minimum_games=(
            "Minimum games in that statistic required to qualify. "
            f"Defaults to {DEFAULT_MINIMUM_GAMES}."
        ),
    )
    @app_commands.choices(
        leaderboard_type=DAILY_LEADERBOARD_CHOICES,
    )
    @bot_admin_only()
    async def set_daily(
        self,
        interaction: discord.Interaction,
        leaderboard_type: app_commands.Choice[str],
        channel: discord.TextChannel,
        time: str,
        minimum_games: app_commands.Range[int, 0, 999]
        = DEFAULT_MINIMUM_GAMES,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        timezone_name = await get_guild_timezone(
            interaction.guild_id
        )

        if timezone_name is None:
            await interaction.response.send_message(
                (
                    "Set this server's timezone first with "
                    "`/server timezone`."
                ),
                ephemeral=True,
            )
            return

        clean_time = time.strip()

        try:
            datetime.strptime(
                clean_time,
                "%H:%M",
            )
        except ValueError:
            await interaction.response.send_message(
                (
                    "Time must use 24-hour `HH:MM` format, "
                    "for example `20:00`."
                ),
                ephemeral=True,
            )
            return

        await set_daily_leaderboard_config(
            guild_id=interaction.guild_id,
            leaderboard_type=leaderboard_type.value,
            channel_id=channel.id,
            post_time=clean_time,
            minimum_games=minimum_games,
            # Retained for backward-compatible database storage.
            # Daily rendering now automatically uses the maximum number
            # of rows that fit in one embed instead of a fixed top count.
            top_count=500,
        )

        await interaction.response.send_message(
            (
                f"Daily **{DISPLAY_NAMES[leaderboard_type.value]}** "
                f"leaderboard enabled in {channel.mention}.\n"
                f"It will post each day at `{clean_time}` "
                f"in `{timezone_name}`.\n"
                f"Minimum games: **{minimum_games}**.\n"
                "The automatic post will show the maximum number of "
                "players that fit in one Discord embed.\n"
                "Other configured daily leaderboard channels remain enabled."
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="info",
        description="Shows all daily leaderboard settings in this server.",
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

        configs = await get_daily_leaderboard_configs(
            interaction.guild_id
        )
        timezone_name = await get_guild_timezone(
            interaction.guild_id
        )

        enabled_configs = [
            config
            for config in configs
            if bool(config["enabled"])
        ]

        if not enabled_configs:
            await interaction.response.send_message(
                "No daily leaderboards are currently enabled in this server.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="Daily Leaderboards",
            description=(
                f"Timezone: `{timezone_name or 'Not configured'}`\n"
                f"**{len(enabled_configs)} enabled configuration(s)**"
            ),
            color=discord.Color.blurple(),
        )

        for config in enabled_configs:
            embed.add_field(
                name=DISPLAY_NAMES[
                    str(config["leaderboard_type"])
                ],
                value=(
                    f"**Channel:** <#{config['channel_id']}>\n"
                    f"**Time:** `{config['post_time']}`\n"
                    f"**Minimum games:** {config['minimum_games']}\n"
                    "**Players shown:** Maximum that fits in one embed"
                ),
                inline=False,
            )

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True,
        )

    @app_commands.command(
        name="disable",
        description="Disables one channel's daily leaderboard or all of them.",
    )
    @app_commands.describe(
        channel=(
            "Daily leaderboard channel to disable. "
            "Leave blank to disable all daily leaderboards."
        ),
    )
    @bot_admin_only()
    async def disable(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        disabled_count = await disable_daily_leaderboard(
            interaction.guild_id,
            channel.id if channel is not None else None,
        )

        if disabled_count == 0:
            await interaction.response.send_message(
                (
                    "That channel does not have an enabled daily leaderboard."
                    if channel is not None
                    else "All daily leaderboards are already disabled."
                ),
                ephemeral=True,
            )
            return

        if channel is not None:
            message = (
                f"The daily leaderboard in {channel.mention} has been disabled."
            )
        else:
            message = (
                f"Disabled **{disabled_count}** daily leaderboard"
                f"{'s' if disabled_count != 1 else ''} in this server."
            )

        await interaction.response.send_message(
            message,
            ephemeral=True,
        )

    @tasks.loop(minutes=1)
    async def daily_post_loop(self) -> None:
        configs = await get_enabled_daily_leaderboards()

        for config in configs:
            try:
                guild_id = int(config["guild_id"])
                timezone_name = str(config["timezone"])
                local_zone = ZoneInfo(timezone_name)
                now_local = datetime.now(local_zone)
                local_date = now_local.date().isoformat()

                post_hour, post_minute = (
                    int(part)
                    for part in str(
                        config["post_time"]
                    ).split(":", maxsplit=1)
                )

                scheduled_minutes = (
                    post_hour * 60
                    + post_minute
                )
                now_minutes = (
                    now_local.hour * 60
                    + now_local.minute
                )

                # If the bot was offline at the exact scheduled minute,
                # post once it comes back later that same local day.
                if now_minutes < scheduled_minutes:
                    continue

                if (
                    config["last_post_local_date"]
                    == local_date
                ):
                    continue

                failure_key = (
                    int(config["id"]),
                    local_date,
                )

                if failure_key in self._failed_daily_attempts:
                    continue

                await self._post_daily_for_config(
                    config=config,
                    guild_id=guild_id,
                    timezone_name=timezone_name,
                    local_date=local_date,
                )

            except Exception as error:
                print(
                    "Daily leaderboard scheduler error:",
                    repr(error),
                )

    async def _post_daily_for_config(
        self,
        *,
        config,
        guild_id: int,
        timezone_name: str,
        local_date: str,
    ) -> None:
        leaderboard_cog = self._leaderboard_cog()

        if leaderboard_cog is None:
            return

        season = await get_active_season(
            guild_id
        )

        if season is None:
            return

        players = await get_season_players(
            season["id"]
        )

        if not players:
            return

        # The daily post normally forces a fresh snapshot. If a command
        # refreshed the same guild within the last five minutes, reuse it
        # to avoid scraping everyone twice at essentially the same time.
        (
            profiles,
            _,
            _,
            _,
        ) = await leaderboard_cog.get_leaderboard_profiles(
            guild_id=guild_id,
            season_id=int(season["id"]),
            players=players,
            force_refresh=True,
            allow_recent_force_reuse=True,
        )

        metric = str(config["leaderboard_type"])
        minimum_games = int(
            config["minimum_games"]
        )
        entries = leaderboard_cog.build_entries(
            profiles=profiles,
            tracked_player_count=int(
                season["tracked_player_count"]
            ),
            metric=metric,
            minimum_games=minimum_games,
        )

        embed = build_daily_embed(
            season_number=int(
                season["season_number"]
            ),
            entries=entries,
            metric=metric,
            minimum_games=minimum_games,
            timezone_name=timezone_name,
            post_time=str(config["post_time"]),
        )

        channel_id = int(config["channel_id"])

        channel = self.bot.get_channel(
            channel_id
        )

        if channel is None:
            try:
                channel = await self.bot.fetch_channel(
                    channel_id
                )
            except discord.DiscordException as error:
                self._failed_daily_attempts.add(
                    (
                        int(config["id"]),
                        local_date,
                    )
                )
                print(
                    (
                        "Could not fetch daily leaderboard "
                        f"channel {channel_id}: {error}"
                    )
                )
                return

        if not isinstance(
            channel,
            (
                discord.TextChannel,
                discord.Thread,
            ),
        ):
            self._failed_daily_attempts.add(
                (
                    guild_id,
                    local_date,
                )
            )
            return

        try:
            await channel.send(embed=embed)
        except discord.DiscordException as error:
            self._failed_daily_attempts.add(
                (
                    guild_id,
                    local_date,
                )
            )
            print(
                (
                    "Could not send daily leaderboard "
                    f"for guild {guild_id}: {error}"
                )
            )
            return

        await mark_daily_leaderboard_posted(
            int(config["id"]),
            local_date,
        )

    @daily_post_loop.before_loop
    async def before_daily_post_loop(
        self,
    ) -> None:
        await self.bot.wait_until_ready()

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.CheckFailure):
            message = (
                "You do not have bot administrator permission "
                "for this server."
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


async def setup(
    bot: commands.Bot,
) -> None:
    await bot.add_cog(
        LeaderboardCommands(bot)
    )
    await bot.add_cog(
        ServerConfigCommands(bot)
    )
    await bot.add_cog(
        DailyLeaderboardCommands(bot)
    )