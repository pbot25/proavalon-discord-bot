import asyncio
from datetime import datetime, timezone
import math

import discord
from discord import app_commands
from discord.ext import commands

from database.account_links import get_preferred_link_for_discord
from database.known_players import get_known_proavalon_usernames
from database.ratings import get_cached_rating
from database.voting_metrics import get_player_voting_metrics
from database.social_stats import (
    MIN_SHARED_GAMES,
    build_pair_social_stats,
    build_player_social_stats,
    get_season_matches,
)

from database.seasons import (
    get_active_season,
    get_season_players,
    is_season_player_voided,
    register_season_player,
    season_display_name,
)
from utils.discord_users import parse_discord_user_id
from proavalon.profile_scraper import (
    PlayerProfile,
    ProAvalonError,
    ProAvalonLoginError,
    ProAvalonUserNotFound,
)


DEFAULT_TRACKED_PLAYER_COUNT = 10

RATING_BRACKET_EMOJIS = {
    "iron": "⚙️",
    "bronze": "🥉",
    "silver": "🥈",
    "gold": "🥇",
    "platinum": "🔷",
    "diamond": "💎",
    "champion": "🏆",
}


SOCIAL_LIST_CHOICES = [
    app_commands.Choice(
        name="Most Played With",
        value="most-played-with",
    ),
    app_commands.Choice(
        name="Teammates",
        value="teammates",
    ),
    app_commands.Choice(
        name="Best Teammates",
        value="best-teammates",
    ),
    app_commands.Choice(
        name="Best Resistance Teammates",
        value="best-resistance-teammates",
    ),
    app_commands.Choice(
        name="Best Spy Teammates",
        value="best-spy-teammates",
    ),
    app_commands.Choice(
        name="Rivals",
        value="rivals",
    ),
    app_commands.Choice(
        name="Most Dominated",
        value="most-dominated",
    ),
]


HISTORY_ROLE_CHOICES = [
    app_commands.Choice(name=name, value=value)
    for name, value in (
        ("VT", "vt"),
        ("Lover", "lover"),
        ("Percival", "percival"),
        ("Merlin", "merlin"),
        ("Assassin", "assassin"),
        ("Morgana", "morgana"),
        ("Mordred", "mordred"),
        ("Oberon", "oberon"),
        ("Overall Resistance", "overall-resistance"),
        ("Overall Spy", "overall-spy"),
    )
]


class SocialListView(discord.ui.View):
    def __init__(
        self,
        *,
        title: str,
        lines: list[str],
        season_name: str,
        minimum_games: int,
        page_size: int,
        requesting_user_id: int,
    ) -> None:
        super().__init__(timeout=180)
        self.title = title
        self.lines = lines
        self.season_name = season_name
        self.minimum_games = minimum_games
        self.page_size = page_size
        self.requesting_user_id = requesting_user_id
        self.page = 0
        self.total_pages = max(1, math.ceil(len(lines) / page_size))
        self._update_buttons()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requesting_user_id:
            return True
        await interaction.response.send_message(
            "Only the person who opened this list can change pages.",
            ephemeral=True,
        )
        return False

    def _update_buttons(self) -> None:
        self.previous_button.disabled = self.page == 0
        self.next_button.disabled = self.page >= self.total_pages - 1

    def build_embed(self) -> discord.Embed:
        start = self.page * self.page_size
        page_lines = self.lines[start:start + self.page_size]
        embed = discord.Embed(
            title=self.title,
            description=(
                "\n".join(page_lines)
                if page_lines
                else "No players meet the current minimum-games requirement yet."
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(
            text=(
                f"{self.season_name} • Minimum: {self.minimum_games} game(s) • "
                f"Page {self.page + 1}/{self.total_pages} • "
                f"{len(self.lines)} result(s) • {self.page_size}/page"
            )
        )
        return embed

    @discord.ui.button(
        label="Previous", emoji="◀️", style=discord.ButtonStyle.secondary
    )
    async def previous_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if self.page > 0:
            self.page -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(
        label="Next", emoji="▶️", style=discord.ButtonStyle.secondary
    )
    async def next_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if self.page < self.total_pages - 1:
            self.page += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True


def get_rating_bracket_emoji(
    rating_bracket: str | None,
) -> str:
    """Return the display emoji for a ProAvalon rating bracket."""

    if rating_bracket is None:
        return ""

    return RATING_BRACKET_EMOJIS.get(
        rating_bracket.casefold(),
        "",
    )


def format_rating_bracket(
    rating_bracket: str | None,
) -> str:
    """Format a ProAvalon rating bracket for Discord."""

    if rating_bracket is None:
        return "Unranked"

    emoji = get_rating_bracket_emoji(rating_bracket)

    if emoji:
        return f"{emoji} {rating_bracket}"

    return rating_bracket

COMPARE_METRIC_ORDER = [
    "Overall WR",
    "Resistance WR",
    "Spy WR",
    "VT",
    "Lover",
    "Percival",
    "Merlin",
    "Assassin",
    "Morgana",
    "Mordred",
    "Oberon",
]


def format_play_time(
    seconds: float | None,
) -> str:
    """Format elapsed seconds as a compact hours/minutes string."""

    if seconds is None:
        return "Unavailable"

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


def format_rating_age(
    updated_at: str | None,
) -> str:
    """Format an ISO timestamp as a compact relative age."""

    if not updated_at:
        return "unknown"

    try:
        updated = datetime.fromisoformat(
            updated_at.replace("Z", "+00:00")
        )
    except ValueError:
        return "unknown"

    if updated.tzinfo is None:
        updated = updated.replace(
            tzinfo=timezone.utc
        )

    seconds = max(
        0,
        int(
            (
                datetime.now(timezone.utc)
                - updated.astimezone(timezone.utc)
            ).total_seconds()
        ),
    )

    if seconds < 60:
        return "just now"

    minutes = seconds // 60

    if minutes < 60:
        return f"{minutes}m ago"

    hours = minutes // 60

    if hours < 48:
        return f"{hours}h ago"

    days = hours // 24
    return f"{days}d ago"


def format_record(wins: int, losses: int, win_rate: float) -> str:
    games = wins + losses
    return (
        f"**Games:** {games:,}\n"
        f"**Record:** {wins:,}W – {losses:,}L\n"
        f"**Win rate:** {win_rate:.1f}%"
    )


def calculate_win_rate(wins: int, losses: int) -> float:
    games = wins + losses
    return 0.0 if games == 0 else wins / games * 100


def calculate_optional_win_rate(
    wins: int,
    losses: int,
) -> float | None:
    games = wins + losses
    return None if games == 0 else wins / games * 100


def format_role_name(role_name: str) -> str:
    return role_name.replace("_", " ").title()


def get_role_alignment(
    profile: PlayerProfile,
    role_name: str,
) -> str | None:
    target = role_name.casefold()

    for stored_role, alignment in profile.role_alliances.items():
        if stored_role.casefold() == target:
            return alignment

    return None


def get_game_size_records(
    profile: PlayerProfile,
    game_size_key: str,
) -> tuple[int, int, int, int, int, int]:
    roles = profile.role_stats.get(game_size_key, {})

    overall_wins = 0
    overall_losses = 0
    resistance_wins = 0
    resistance_losses = 0
    spy_wins = 0
    spy_losses = 0

    for role_name, record in roles.items():
        overall_wins += record.wins
        overall_losses += record.losses

        alignment = get_role_alignment(profile, role_name)

        if alignment is None:
            continue

        if alignment.casefold() == "resistance":
            resistance_wins += record.wins
            resistance_losses += record.losses
        elif alignment.casefold() == "spy":
            spy_wins += record.wins
            spy_losses += record.losses

    return (
        overall_wins,
        overall_losses,
        resistance_wins,
        resistance_losses,
        spy_wins,
        spy_losses,
    )


def get_game_size_games_played(
    profile: PlayerProfile,
    game_size_key: str,
) -> int:
    roles = profile.role_stats.get(game_size_key, {})
    return sum(record.games_played for record in roles.values())


def build_role_field_value(
    profile: PlayerProfile,
    game_size_key: str,
) -> str:
    roles = profile.role_stats.get(game_size_key, {})

    if not roles:
        return f"No {game_size_key} role statistics are available."

    combined_roles: dict[str, dict[str, int]] = {}

    for role_name, record in roles.items():
        normalized_role = role_name.casefold()

        if normalized_role in {"isolde", "tristan"}:
            display_name = "Lover"
        elif normalized_role == "resistance":
            display_name = "VT"
        else:
            display_name = format_role_name(role_name)

        if display_name not in combined_roles:
            combined_roles[display_name] = {
                "games": 0,
                "wins": 0,
                "losses": 0,
            }

        combined_roles[display_name]["games"] += record.games_played
        combined_roles[display_name]["wins"] += record.wins
        combined_roles[display_name]["losses"] += record.losses

    resistance_order = ["VT", "Lover", "Percival", "Merlin"]
    spy_order = ["Assassin", "Morgana", "Mordred", "Oberon"]

    def build_section(title: str, role_order: list[str]) -> str:
        rows: list[tuple[str, int, int, int, float]] = []

        for role_name in role_order:
            stats = combined_roles.get(role_name)

            if stats is None:
                continue

            games = stats["games"]
            wins = stats["wins"]
            losses = stats["losses"]
            rows.append(
                (
                    role_name,
                    games,
                    wins,
                    losses,
                    calculate_win_rate(wins, losses),
                )
            )

        if not rows:
            return f"{title}\nNo statistics available."

        role_width = max(
            len("Role"),
            max(len(role_name) for role_name, *_ in rows),
        )
        games_width = max(
            len("GP"),
            max(len(f"{games:,}") for _, games, *_ in rows),
        )
        record_width = max(
            len("W-L"),
            max(
                len(f"{wins:,}-{losses:,}")
                for _, _, wins, losses, _ in rows
            ),
        )
        win_rate_width = max(
            len("WR"),
            max(
                len(f"{win_rate:.1f}%")
                for *_, win_rate in rows
            ),
        )

        lines = [
            title,
            (
                f"{'Role':<{role_width}}  "
                f"{'GP':>{games_width}}  "
                f"{'W-L':>{record_width}}  "
                f"{'WR':>{win_rate_width}}"
            ),
            (
                f"{'-' * role_width}  "
                f"{'-' * games_width}  "
                f"{'-' * record_width}  "
                f"{'-' * win_rate_width}"
            ),
        ]

        for role_name, games, wins, losses, win_rate in rows:
            record_text = f"{wins:,}-{losses:,}"
            win_rate_text = f"{win_rate:.1f}%"
            lines.append(
                f"{role_name:<{role_width}}  "
                f"{games:>{games_width},}  "
                f"{record_text:>{record_width}}  "
                f"{win_rate_text:>{win_rate_width}}"
            )

        return "\n".join(lines)

    table = (
        "```\n"
        f"{build_section('RESISTANCE', resistance_order)}\n\n"
        f"{build_section('SPY', spy_order)}\n"
        "```"
    )

    return (
        table
        if len(table) <= 1024
        else "The role table is too large to display."
    )


def build_profile_embed(
    profile: PlayerProfile,
    cache_hit: bool,
    tracked_player_count: int,
    season_name: str | None,
    numeric_rating: int | None = None,
    rating_updated_at: str | None = None,
    live_rating_bracket: str | None = None,
) -> discord.Embed:
    game_size_key = f"{tracked_player_count}p"

    (
        overall_wins,
        overall_losses,
        resistance_wins,
        resistance_losses,
        spy_wins,
        spy_losses,
    ) = get_game_size_records(profile, game_size_key)

    total_games = overall_wins + overall_losses

    if season_name is None:
        description = (
            f"Statistics for `{profile.username}`. "
            f"Only {tracked_player_count}-player games are shown."
        )
    else:
        description = (
            f"{season_name} statistics for "
            f"`{profile.username}`. "
            f"Only {tracked_player_count}-player games are counted."
        )

    embed = discord.Embed(
        title=(
            f"{profile.username} — "
            f"{tracked_player_count}-Player Statistics"
        ),
        description=description,
        color=discord.Color.blurple(),
    )

    if total_games == 0:
        embed.add_field(
            name=f"{tracked_player_count}-Player Statistics",
            value=(
                f"No {tracked_player_count}-player statistics "
                "are available for this account."
            ),
            inline=False,
        )
    else:
        embed.add_field(
            name="Overall",
            value=format_record(
                overall_wins,
                overall_losses,
                calculate_win_rate(overall_wins, overall_losses),
            ),
            inline=True,
        )
        embed.add_field(
            name="Resistance",
            value=format_record(
                resistance_wins,
                resistance_losses,
                calculate_win_rate(
                    resistance_wins,
                    resistance_losses,
                ),
            ),
            inline=True,
        )
        embed.add_field(
            name="Spy",
            value=format_record(
                spy_wins,
                spy_losses,
                calculate_win_rate(spy_wins, spy_losses),
            ),
            inline=True,
        )
        embed.add_field(
            name=f"{game_size_key} Role Statistics",
            value=build_role_field_value(profile, game_size_key),
            inline=False,
        )

    embed.add_field(
        name="Total Games",
        value=f"{profile.total_games_played:,}",
        inline=True,
    )

    embed.add_field(
        name="Total Time Played",
        value=format_play_time(
            profile.total_time_played_seconds
        ),
        inline=True,
    )

    if profile.date_joined:
        embed.add_field(
            name="Joined ProAvalon",
            value=profile.date_joined[:10],
            inline=True,
        )

    if profile.nationality:
        embed.add_field(
            name="Nationality",
            value=profile.nationality,
            inline=True,
        )

    embed.add_field(
        name="Rank",
        value=format_rating_bracket(
            live_rating_bracket
            or profile.rating_bracket
        ),
        inline=True,
    )

    rating_value = (
        f"{numeric_rating:,}"
        if numeric_rating is not None
        else "Not observed yet"
    )

    if numeric_rating is not None:
        rating_value += (
            "\n"
            f"Last seen: "
            f"{format_rating_age(rating_updated_at)}"
        )

    embed.add_field(
        name="Rating",
        value=rating_value,
        inline=True,
    )

    thumbnail = profile.avatar_res_url or profile.avatar_spy_url

    if thumbnail:
        embed.set_thumbnail(url=thumbnail)

    source_text = (
        "Served from the 15-minute cache"
        if cache_hit
        else "Freshly retrieved from ProAvalon"
    )

    embed.set_footer(
        text=(
            f"{source_text} • "
            f"Only {tracked_player_count}-player games counted"
        )
    )

    return embed


async def player_username_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    """
    Autocomplete /stats player from usernames this server already knows.

    Discord supports at most 25 autocomplete choices, so an empty field
    shows the first 25 alphabetical usernames and typing filters the full
    known-user pool down to the first 25 matches.

    Voided players from the active season are omitted because /stats
    player is blocked for them while that season is active.
    """

    if interaction.guild_id is None:
        return []

    usernames = await get_known_proavalon_usernames(
        interaction.guild_id
    )

    active_season = await get_active_season(
        interaction.guild_id
    )

    if active_season is not None:
        filtered_usernames: list[str] = []

        for username in usernames:
            if await is_season_player_voided(
                int(active_season["id"]),
                username,
            ):
                continue

            filtered_usernames.append(username)

        usernames = filtered_usernames

    normalized_current = current.strip().casefold()

    if normalized_current:
        usernames = [
            username
            for username in usernames
            if normalized_current in username.casefold()
        ]

    return [
        app_commands.Choice(
            name=username,
            value=username,
        )
        for username in usernames[:25]
    ]


async def registered_username_autocomplete(
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

    players = await get_season_players(season["id"])
    normalized_current = current.strip().casefold()

    matches = [
        player["proavalon_username"]
        for player in players
        if normalized_current
        in player["proavalon_username"].casefold()
    ]

    return [
        app_commands.Choice(name=username, value=username)
        for username in matches[:25]
    ]


async def resolve_registered_username(
    season_id: int,
    requested_username: str,
) -> str | None:
    players = await get_season_players(season_id)
    requested = requested_username.strip().casefold()

    for player in players:
        username = player["proavalon_username"]

        if username.casefold() == requested:
            return username

    return None


def get_comparison_metrics(
    profile: PlayerProfile,
    game_size_key: str,
) -> dict[str, float | None]:
    (
        overall_wins,
        overall_losses,
        resistance_wins,
        resistance_losses,
        spy_wins,
        spy_losses,
    ) = get_game_size_records(profile, game_size_key)

    metrics: dict[str, float | None] = {
        "Overall WR": calculate_optional_win_rate(
            overall_wins,
            overall_losses,
        ),
        "Resistance WR": calculate_optional_win_rate(
            resistance_wins,
            resistance_losses,
        ),
        "Spy WR": calculate_optional_win_rate(
            spy_wins,
            spy_losses,
        ),
        "VT": None,
        "Lover": None,
        "Percival": None,
        "Merlin": None,
        "Assassin": None,
        "Morgana": None,
        "Mordred": None,
        "Oberon": None,
    }

    totals = {
        role_name: {"wins": 0, "losses": 0}
        for role_name in COMPARE_METRIC_ORDER[3:]
    }

    roles = profile.role_stats.get(game_size_key, {})

    for role_name, record in roles.items():
        normalized = role_name.casefold()

        mapping = {
            "resistance": "VT",
            "isolde": "Lover",
            "tristan": "Lover",
            "percival": "Percival",
            "merlin": "Merlin",
            "assassin": "Assassin",
            "morgana": "Morgana",
            "mordred": "Mordred",
            "oberon": "Oberon",
        }

        display_name = mapping.get(normalized)

        if display_name is None:
            continue

        totals[display_name]["wins"] += record.wins
        totals[display_name]["losses"] += record.losses

    for role_name, role_totals in totals.items():
        metrics[role_name] = calculate_optional_win_rate(
            role_totals["wins"],
            role_totals["losses"],
        )

    return metrics


def format_compare_value(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.1f}%"


def build_compare_table(
    profiles: list[PlayerProfile],
    tracked_player_count: int,
) -> str:
    """
    Build a compact comparison table for exactly two players.

    P1 and P2 keep the table narrow and prevent Discord from wrapping.
    The Winner column uses colored emoji indicators:
    🔵 for P1, 🔴 for P2, and ⚪ for a tie.
    """

    game_size_key = f"{tracked_player_count}p"

    metric_sets = [
        get_comparison_metrics(
            profile=profile,
            game_size_key=game_size_key,
        )
        for profile in profiles
    ]

    statistic_width = max(
        len("Statistic"),
        max(
            len(metric_name)
            for metric_name in COMPARE_METRIC_ORDER
        ),
    ) + 2

    player_width = 9

    header = (
        f"{'Statistic':<{statistic_width}}"
        f"{'P1':<{player_width}}"
        f"{'P2':<{player_width}}"
        "Winner"
    )

    divider = (
        f"{'-' * (statistic_width - 2)}  "
        f"{'-' * (player_width - 2)}  "
        f"{'-' * (player_width - 2)}  "
        "------"
    )

    lines = [header, divider]

    for metric_name in COMPARE_METRIC_ORDER:
        values = [
            metric_set[metric_name]
            for metric_set in metric_sets
        ]

        first_value = values[0]
        second_value = values[1]

        if first_value is None and second_value is None:
            winner_text = "—"
        elif second_value is None:
            winner_text = "🔵"
        elif first_value is None:
            winner_text = "🔴"
        elif first_value > second_value:
            winner_text = "🔵"
        elif second_value > first_value:
            winner_text = "🔴"
        else:
            winner_text = "⚪"

        lines.append(
            f"{metric_name:<{statistic_width}}"
            f"{format_compare_value(first_value):<{player_width}}"
            f"{format_compare_value(second_value):<{player_width}}"
            f"{winner_text}"
        )

        if metric_name in {"Spy WR", "Merlin"}:
            lines.append("")

    return "```\n" + "\n".join(lines) + "\n```"


def build_compare_embed(
    profiles: list[PlayerProfile],
    tracked_player_count: int,
) -> discord.Embed:
    """Create the two-player comparison embed."""

    player_legend = (
        f"🔵 **P1:** `{profiles[0].username}` — "
        f"{format_rating_bracket(profiles[0].rating_bracket)}\n"
        f"🔴 **P2:** `{profiles[1].username}` — "
        f"{format_rating_bracket(profiles[1].rating_bracket)}"
    )

    table = build_compare_table(
        profiles=profiles,
        tracked_player_count=tracked_player_count,
    )

    embed = discord.Embed(
        title="Player Statistics Comparison",
        description=(
            f"Comparing 2 registered players using "
            f"{tracked_player_count}-player win rates.\n\n"
            f"{player_legend}\n\n"
            f"{table}"
        ),
        color=discord.Color.blurple(),
    )

    embed.set_footer(
        text=(
            f"Only {tracked_player_count}-player games counted • "
            "⚪ indicates a tie"
        )
    )

    return embed


def format_social_date(value: str | None) -> str:
    if not value:
        return "Unknown"

    try:
        parsed = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )
        return parsed.strftime("%b %d, %Y")
    except ValueError:
        return value[:10]


def social_record_line(
    relation: dict | None,
    *,
    games_key: str,
    wins_key: str,
    losses_key: str,
    rate_key: str,
) -> str:
    if relation is None:
        return "Not enough tracked games yet."

    return (
        f"`{relation['username']}` — "
        f"{relation[wins_key]}W–{relation[losses_key]}L • "
        f"{relation[rate_key]:.1f}% WR • "
        f"{relation[games_key]} games"
    )


def build_social_embed(
    *,
    season_name: str,
    stats: dict,
) -> discord.Embed:
    embed = discord.Embed(
        title=(
            f"{stats['username']} — Social Stats"
        ),
        description=(
            f"{season_name}\n"
            f"Tracking since **{format_social_date(stats['tracking_since'])}** • "
            f"**{stats['matches_analyzed']} matches analyzed**"
        ),
        color=discord.Color.blurple(),
    )

    most_played = stats.get("most_played")
    embed.add_field(
        name="Most Played With",
        value=(
            "No shared matches yet."
            if most_played is None
            else (
                f"`{most_played['username']}` — "
                f"**{most_played['shared_games']} games**"
            )
        ),
        inline=False,
    )

    most_allied = stats.get("most_allied")
    most_opposing = stats.get("most_opposing")

    embed.add_field(
        name="Most Allied Player",
        value=(
            "Not enough tracked games yet."
            if most_allied is None
            else (
                f"`{most_allied['username']}` — "
                f"**{most_allied['same_pct']:.1f}%** same alignment "
                f"({most_allied['same_games']}/{most_allied['shared_games']})"
            )
        ),
        inline=False,
    )

    embed.add_field(
        name="Most Opposing Player",
        value=(
            "Not enough tracked games yet."
            if most_opposing is None
            else (
                f"`{most_opposing['username']}` — "
                f"**{most_opposing['opposite_pct']:.1f}%** opposite alignment "
                f"({most_opposing['opposite_games']}/{most_opposing['shared_games']})"
            )
        ),
        inline=False,
    )

    embed.add_field(
        name="Best Teammate",
        value=social_record_line(
            stats.get("best_teammate"),
            games_key="same_games",
            wins_key="same_wins",
            losses_key="same_losses",
            rate_key="same_win_rate",
        ),
        inline=False,
    )

    embed.add_field(
        name="Worst Teammate",
        value=social_record_line(
            stats.get("worst_teammate"),
            games_key="same_games",
            wins_key="same_wins",
            losses_key="same_losses",
            rate_key="same_win_rate",
        ),
        inline=False,
    )

    nemesis = stats.get("nemesis")
    embed.add_field(
        name="Nemesis",
        value=(
            "Not enough tracked opponent games yet."
            if nemesis is None
            else (
                f"`{nemesis['username']}` — "
                f"{nemesis['opponent_wins']}W–{nemesis['opponent_losses']}L • "
                f"**{100.0 - nemesis['opponent_win_rate']:.1f}% loss rate** • "
                f"{nemesis['opposite_games']} opponent games"
            )
        ),
        inline=False,
    )

    dominated = stats.get("most_dominated")
    embed.add_field(
        name="Most Dominated",
        value=(
            "Not enough tracked opponent games yet."
            if dominated is None
            else (
                f"`{dominated['username']}` — "
                f"{dominated['opponent_wins']}W–{dominated['opponent_losses']}L • "
                f"**{dominated['opponent_win_rate']:.1f}% WR** • "
                f"{dominated['opposite_games']} opponent games"
            )
        ),
        inline=False,
    )

    embed.add_field(
        name="Best Resistance Partner",
        value=social_record_line(
            stats.get("best_res_partner"),
            games_key="res_games",
            wins_key="res_wins",
            losses_key="res_losses",
            rate_key="res_win_rate",
        ),
        inline=False,
    )

    embed.add_field(
        name="Best Spy Partner",
        value=social_record_line(
            stats.get("best_spy_partner"),
            games_key="spy_games",
            wins_key="spy_wins",
            losses_key="spy_losses",
            rate_key="spy_win_rate",
        ),
        inline=False,
    )

    embed.set_footer(
        text=(
            f"Rate-based rankings require at least {stats['minimum_shared_games']} "
            "qualifying games • Uses archived match history only"
        )
    )
    return embed


def build_social_compare_embed(
    *,
    season_name: str,
    stats: dict,
) -> discord.Embed:
    p1 = stats["player_one"]
    p2 = stats["player_two"]

    embed = discord.Embed(
        title=f"{p1} + {p2} — Social Comparison",
        description=(
            f"{season_name}\n"
            f"Tracking since **{format_social_date(stats['tracking_since'])}** • "
            f"**{stats['shared_games']} shared matches**"
        ),
        color=discord.Color.blurple(),
    )

    embed.add_field(
        name="Alignment Frequency",
        value=(
            f"**Same:** {stats['same_pct']:.1f}% "
            f"({stats['same_games']}/{stats['shared_games']})\n"
            f"**Opposite:** {stats['opposite_pct']:.1f}% "
            f"({stats['opposite_games']}/{stats['shared_games']})"
        ),
        inline=False,
    )

    embed.add_field(
        name="Same-Team Record",
        value=(
            f"{stats['same_wins']}W–{stats['same_losses']}L • "
            f"{stats['same_win_rate']:.1f}% WR • "
            f"{stats['same_games']} games"
        ),
        inline=False,
    )

    embed.add_field(
        name="Head-to-Head",
        value=(
            f"`{p1}`: **{stats['player_one_opponent_wins']} wins**\n"
            f"`{p2}`: **{stats['player_two_opponent_wins']} wins**\n"
            f"{stats['opposite_games']} opposite-alignment games"
        ),
        inline=False,
    )

    embed.add_field(
        name="Resistance Together",
        value=(
            f"{stats['res_wins']}W–{stats['res_losses']}L • "
            f"{stats['res_win_rate']:.1f}% WR • "
            f"{stats['res_games']} games"
        ),
        inline=False,
    )

    embed.add_field(
        name="Spies Together",
        value=(
            f"{stats['spy_wins']}W–{stats['spy_losses']}L • "
            f"{stats['spy_win_rate']:.1f}% WR • "
            f"{stats['spy_games']} games"
        ),
        inline=False,
    )

    embed.set_footer(
        text="Uses all archived matches currently stored for this season"
    )
    return embed




async def resolve_stats_target(
    interaction: discord.Interaction,
    season,
    *,
    username: str | None = None,
    discord_user: discord.Member | None = None,
    default_to_invoker: bool = True,
) -> tuple[str | None, str | None]:
    """
    Resolve either a typed ProAvalon username or a linked Discord @.
    Returns (username, error_message).
    """
    has_username = username is not None and bool(username.strip())
    if has_username and discord_user is not None:
        return None, "Choose either `username` or `discord_user`, not both."

    if has_username:
        return username.strip(), None

    target_member = discord_user
    if target_member is None and default_to_invoker:
        target_id = interaction.user.id
        target_mention = interaction.user.mention
    elif target_member is not None:
        target_id = target_member.id
        target_mention = target_member.mention
    else:
        return None, "Provide a username or linked Discord user."

    link = await get_preferred_link_for_discord(
        interaction.guild_id,
        target_id,
        int(season["season_number"]),
    )
    if link is None:
        return (
            None,
            f"{target_mention} is not linked to an account for "
            f"{season_display_name(season)}.",
        )

    return str(link["proavalon_username"]), None


class StatsCommands(
    commands.GroupCog,
    group_name="stats",
    group_description="View ProAvalon player statistics.",
):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="history",
        description="Shows a player's newest archived wins and losses.",
    )
    @app_commands.describe(
        username="Registered player. Leave blank to use your linked account.",
        discord_user="Linked Discord @ to look up.",
        games="Number of recent qualifying games to show. Defaults to 10.",
        role="Optionally show results only for one role.",
    )
    @app_commands.autocomplete(username=registered_username_autocomplete)
    @app_commands.choices(role=HISTORY_ROLE_CHOICES)
    async def history(
        self,
        interaction: discord.Interaction,
        username: str | None = None,
        discord_user: discord.Member | None = None,
        games: app_commands.Range[int, 1, 250] = 10,
        role: app_commands.Choice[str] | None = None,
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

        requested, error = await resolve_stats_target(
            interaction, season, username=username, discord_user=discord_user
        )
        if error is not None:
            await interaction.response.send_message(error, ephemeral=True)
            return

        resolved = await resolve_registered_username(
            season_id=int(season["id"]), requested_username=requested
        )
        if resolved is None:
            await interaction.response.send_message(
                f"`{requested}` is not registered for {season_display_name(season)}.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)
        archived_matches = await get_season_matches(int(season["id"]))
        role_filter = role.value.casefold() if role is not None else None
        results: list[bool] = []

        # Stored matches are oldest-first. Reverse them so the icons read from
        # the newest result to the oldest result.
        for match in reversed(archived_matches):
            participant = next(
                (
                    item for item in match.participants
                    if item.username.casefold() == resolved.casefold()
                ),
                None,
            )
            if participant is None or participant.alignment is None:
                continue
            if role_filter is not None:
                stored_role = str(participant.role or "").replace(" ", "").casefold()
                role_matches = (
                    participant.alignment == "Resistance"
                    if role_filter == "overall-resistance"
                    else participant.alignment == "Spy"
                    if role_filter == "overall-spy"
                    else stored_role == "resistance"
                    if role_filter == "vt"
                    else stored_role in {"tristan", "isolde"}
                    if role_filter == "lover"
                    else stored_role == role_filter
                )
                if not role_matches:
                    continue

            winner = str(match.winner or "").casefold()
            winning_alignment = (
                "Resistance" if "res" in winner
                else "Spy" if "spy" in winner
                else None
            )
            if winning_alignment is not None:
                results.append(participant.alignment == winning_alignment)

        selected = results[:int(games)]
        if not selected:
            role_text = f" as **{role.name}**" if role is not None else ""
            await interaction.followup.send(
                f"No archived results were found for `{resolved}`{role_text}.",
                ephemeral=True,
            )
            return

        wins = sum(selected)
        losses = len(selected) - wins
        win_rate = wins / len(selected) * 100
        icons = ["✅" if won else "❌" for won in selected]
        icon_lines = [
            " ".join(icons[index:index + 25])
            for index in range(0, len(icons), 25)
        ]
        scope = role.name if role is not None else "Overall"
        embed = discord.Embed(
            title=f"{resolved} — Recent History",
            description=(
                f"**{scope} • {len(selected)} most recent game(s)**\n"
                + "\n".join(icon_lines)
                + f"\n\n**{wins}W–{losses}L • {win_rate:.1f}% WR**"
            ),
            color=discord.Color.green() if wins >= losses else discord.Color.red(),
        )
        embed.set_footer(
            text=(
                f"{season_display_name(season)} • Newest → oldest • "
                f"Showing {len(selected)} of {len(results)} qualifying game(s)"
            )
        )
        await interaction.followup.send(embed=embed)

    @app_commands.command(
        name="player",
        description="Displays ProAvalon statistics by username or linked Discord user.",
    )
    @app_commands.describe(
        username="ProAvalon username to look up.",
        discord_user="Linked Discord @ to look up.",
        discord_user_id="Linked Discord user ID to look up.",
    )
    @app_commands.autocomplete(
        username=player_username_autocomplete,
    )
    async def player(
        self,
        interaction: discord.Interaction,
        username: str | None = None,
        discord_user: discord.Member | None = None,
        discord_user_id: str | None = None,
    ) -> None:
        provided_methods = sum(
            value is not None and value != ""
            for value in (username, discord_user, discord_user_id)
        )

        if provided_methods > 1:
            await interaction.response.send_message(
                "Choose only one lookup method: `username`, `discord_user`, or `discord_user_id`.",
                ephemeral=True,
            )
            return

        active_season = (
            await get_active_season(interaction.guild_id)
            if interaction.guild_id is not None
            else None
        )

        if provided_methods == 0:
            if interaction.guild_id is None:
                await interaction.response.send_message(
                    "Use this command in a server, or provide a username.",
                    ephemeral=True,
                )
                return
            if active_season is None:
                await interaction.response.send_message(
                    "No community season is currently active.",
                    ephemeral=True,
                )
                return

            link = await get_preferred_link_for_discord(
                interaction.guild_id,
                interaction.user.id,
                int(active_season["season_number"]),
            )
            if link is None:
                await interaction.response.send_message(
                    (
                        f"{interaction.user.mention} is not linked to an account "
                        f"for {season_display_name(active_season)}."
                    ),
                    ephemeral=True,
                )
                return
            clean_username = str(link["proavalon_username"]).strip()

        elif discord_user is not None or discord_user_id is not None:
            if interaction.guild_id is None:
                await interaction.response.send_message(
                    (
                        "Discord account-link lookups can only "
                        "be used inside a server."
                    ),
                    ephemeral=True,
                )
                return

            active_season_number = (
                int(active_season["season_number"])
                if active_season is not None
                else None
            )
            if discord_user is not None:
                target_discord_id = discord_user.id
                target_discord_mention = discord_user.mention
            else:
                try:
                    target_discord_id = parse_discord_user_id(str(discord_user_id))
                except ValueError as error:
                    await interaction.response.send_message(str(error), ephemeral=True)
                    return
                target_discord_mention = f"<@{target_discord_id}>"

            link = await get_preferred_link_for_discord(
                interaction.guild_id,
                target_discord_id,
                active_season_number,
            )

            if link is None:
                await interaction.response.send_message(
                    f"{target_discord_mention} (`{target_discord_id}`) is not linked to a ProAvalon account in this server.",
                    ephemeral=True,
                )
                return

            clean_username = str(
                link["proavalon_username"]
            ).strip()

        else:
            clean_username = (
                username.strip()
                if username is not None
                else ""
            )

            if not clean_username:
                await interaction.response.send_message(
                    "Enter a ProAvalon username.",
                    ephemeral=True,
                )
                return

        await interaction.response.defer(thinking=True)

        try:
            profile, cache_hit = (
                await self.bot.profile_cache.get_or_fetch(
                    clean_username,
                    self.bot.proavalon_scraper.get_profile_data,
                )
            )
        except ProAvalonUserNotFound:
            await interaction.followup.send(
                f"Could not find the ProAvalon user `{clean_username}`.",
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
                    "Please try again in a few moments."
                ),
                ephemeral=True,
            )
            return
        except ProAvalonError as error:
            await interaction.followup.send(
                f"Could not retrieve that profile: {error}",
                ephemeral=True,
            )
            return
        except ValueError as error:
            await interaction.followup.send(
                str(error),
                ephemeral=True,
            )
            return

        # Outside an active season, /stats player remains a global
        # ProAvalon lookup. During an active season, an account explicitly
        # voided from that season is blocked from /stats player in this
        # server until an administrator unvoids it.
        tracked_player_count = DEFAULT_TRACKED_PLAYER_COUNT
        season_name = None

        if active_season is not None:
            if await is_season_player_voided(
                int(active_season["id"]),
                profile.username,
            ):
                await interaction.followup.send(
                    (
                        f"`{profile.username}` has been voided from "
                        f"{season_display_name(active_season)} and cannot "
                        "be viewed with `/stats player` in this server "
                        "while that season is active."
                    ),
                    ephemeral=True,
                )
                return

            season_player_count = int(
                active_season["tracked_player_count"]
            )
            required_prefix = str(
                active_season["required_prefix"]
            )

            is_prefix_eligible = (
                profile.username.casefold().startswith(
                    required_prefix.casefold()
                )
            )

            if is_prefix_eligible:
                season_game_size_key = (
                    f"{season_player_count}p"
                )
                games_played = get_game_size_games_played(
                    profile,
                    season_game_size_key,
                )

                if games_played >= 1:
                    registered = await register_season_player(
                        community_season_id=active_season["id"],
                        proavalon_username=profile.username,
                    )

                    # A voided username still works as a normal global
                    # /stats lookup, but it is not treated as part of the
                    # active community season.
                    if registered:
                        tracked_player_count = (
                            season_player_count
                        )
                        season_name = season_display_name(active_season)

        rating_snapshot = get_cached_rating(
            profile.username
        )

        profile_embed = build_profile_embed(
                profile=profile,
                cache_hit=cache_hit,
                tracked_player_count=tracked_player_count,
                season_name=season_name,
                numeric_rating=(
                    rating_snapshot.rating
                    if rating_snapshot is not None
                    else None
                ),
                rating_updated_at=(
                    rating_snapshot.rating_updated_at
                    if rating_snapshot is not None
                    else None
                ),
                live_rating_bracket=(
                    rating_snapshot.rating_bracket
                    if rating_snapshot is not None
                    else None
                ),
            )

        if active_season is not None:
            voting = await get_player_voting_metrics(
                int(active_season["id"]), profile.username
            )
            accuracy = (
                f"{voting.overall.accuracy:.1f}% "
                f"({voting.overall.correct_votes}/{voting.overall.eligible_votes})"
                if voting.overall.accuracy is not None
                else "N/A (0 votes)"
            )
            profile_embed.add_field(
                name="Crit Voting Accuracy",
                value=accuracy,
                inline=True,
            )
            profile_embed.add_field(
                name="Crit Errors",
                value=f"{voting.overall.crit_errors:,}",
                inline=True,
            )

        await interaction.followup.send(embed=profile_embed)

    @app_commands.command(
        name="voting",
        description="Shows detailed active-season crit voting metrics by Resistance role.",
    )
    @app_commands.describe(
        username="ProAvalon username. Leave blank to use your linked account.",
        discord_user="Linked Discord @ to look up.",
    )
    async def voting(
        self,
        interaction: discord.Interaction,
        username: str | None = None,
        discord_user: discord.Member | None = None,
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

        clean_username, error = await resolve_stats_target(
            interaction,
            season,
            username=username,
            discord_user=discord_user,
        )
        if error is not None:
            await interaction.response.send_message(
                error,
                ephemeral=True,
            )
            return

        metrics = await get_player_voting_metrics(
            int(season["id"]), clean_username
        )

        def line(label, item):
            accuracy = (
                f"{item.accuracy:.1f}%"
                if item.accuracy is not None else "N/A"
            )
            error_rate = (
                f"{item.error_rate:.1f}%"
                if item.error_rate is not None else "N/A"
            )
            return (
                f"**{label}** — {item.correct_votes}/{item.eligible_votes} "
                f"correct ({accuracy}) • {item.crit_errors} errors ({error_rate})"
            )

        embed = discord.Embed(
            title=f"{metrics.username} — Crit Voting Metrics",
            description=(
                f"**Season Games Played:** {metrics.games_played}\n"
                + line("All Resistance Roles", metrics.overall)
            ),
            color=discord.Color.blurple(),
        )
        for role_name in ("VT", "Lover", "Percival", "Merlin"):
            item = metrics.by_role.get(role_name)
            if item is not None and item.eligible_votes:
                embed.add_field(
                    name=role_name,
                    value=(
                        f"**Votes:** {item.eligible_votes}\n"
                        f"**Accuracy:** {item.accuracy:.1f}% "
                        f"({item.correct_votes}/{item.eligible_votes})\n"
                        f"**Crit Errors:** {item.crit_errors} "
                        f"({item.error_rate:.1f}%)"
                    ),
                    inline=True,
                )
        embed.set_footer(
            text="Crit metrics are reconstructed from archived season vote history."
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(
        name="compare",
        description="Compares two registered season players.",
    )
    @app_commands.describe(
        player_one="Player 1",
        player_two="Player 2",
    )
    @app_commands.autocomplete(
        player_one=registered_username_autocomplete,
        player_two=registered_username_autocomplete,
    )
    async def compare(
        self,
        interaction: discord.Interaction,
        player_one: str,
        player_two: str,
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
                (
                    "No community season is currently active. "
                    "Set a season before comparing registered players."
                ),
                ephemeral=True,
            )
            return

        requested_usernames = [
            player_one.strip(),
            player_two.strip(),
        ]

        resolved_usernames: list[str] = []

        for requested_username in requested_usernames:
            resolved_username = await resolve_registered_username(
                season_id=season["id"],
                requested_username=requested_username,
            )

            if resolved_username is None:
                await interaction.response.send_message(
                    (
                        f"`{requested_username}` is not registered for "
                        f"{season_display_name(season)}."
                    ),
                    ephemeral=True,
                )
                return

            resolved_usernames.append(resolved_username)

        if (
            resolved_usernames[0].casefold()
            == resolved_usernames[1].casefold()
        ):
            await interaction.response.send_message(
                "Choose two different players to compare.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)

        profiles: list[PlayerProfile] = []

        try:
            for username in resolved_usernames:
                profile, _ = (
                    await self.bot.profile_cache.get_or_fetch(
                        username,
                        self.bot.proavalon_scraper.get_profile_data,
                    )
                )

                profiles.append(profile)

        except ProAvalonUserNotFound:
            await interaction.followup.send(
                "Could not find one of the selected profiles.",
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
                f"Could not compare those profiles: {error}",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            embed=build_compare_embed(
                profiles=profiles,
                tracked_player_count=int(
                    season["tracked_player_count"]
                ),
            )
        )


    @app_commands.command(
        name="list",
        description="Lists teammates, rivals, and dominated opponents.",
    )
    @app_commands.describe(
        category="Which relationship list to show.",
        username=(
            "Registered season player. Leave blank to use your linked "
            "active-season account."
        ),
        discord_user="Linked Discord @ to look up.",
        minimum_games=(
            "Minimum games together/opposite required to appear. Defaults to 1."
        ),
        results_per_page="Number of results shown on each page. Defaults to 10.",
    )
    @app_commands.choices(category=SOCIAL_LIST_CHOICES)
    @app_commands.autocomplete(username=registered_username_autocomplete)
    async def relationship_list(
        self,
        interaction: discord.Interaction,
        category: app_commands.Choice[str],
        username: str | None = None,
        discord_user: discord.Member | None = None,
        minimum_games: app_commands.Range[int, 1, 999] = 1,
        results_per_page: app_commands.Range[int, 5, 25] = 10,
    ) -> None:
        if interaction.guild_id is None:
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

        requested, error = await resolve_stats_target(
            interaction,
            season,
            username=username,
            discord_user=discord_user,
        )
        if error is not None:
            await interaction.response.send_message(
                error,
                ephemeral=True,
            )
            return

        resolved = await resolve_registered_username(
            season_id=int(season["id"]),
            requested_username=requested,
        )
        if resolved is None:
            await interaction.response.send_message(
                (
                    f"`{requested}` is not registered for "
                    f"{season_display_name(season)}."
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)

        matches = await get_season_matches(int(season["id"]))
        social = build_player_social_stats(
            matches,
            resolved,
            minimum_shared_games=1,
        )
        if social is None:
            await interaction.followup.send(
                (
                    f"No archived match history has been recorded yet for "
                    f"`{resolved}`."
                ),
                ephemeral=True,
            )
            return

        relationships = list(social["relationships"])
        minimum = int(minimum_games)

        if category.value == "most-played-with":
            relationships = [
                row
                for row in relationships
                if int(row["shared_games"]) >= minimum
            ]
            relationships.sort(
                key=lambda row: (
                    -int(row["shared_games"]),
                    -int(row["same_games"]),
                    str(row["username"]).casefold(),
                )
            )
            title = f"{resolved} — Most Played With"
            lines = [
                (
                    f"**{index}.** `{row['username']}` — "
                    f"**{int(row['shared_games'])} shared game(s)** "
                    f"• {int(row['same_games'])} same "
                    f"• {int(row['opposite_games'])} opposite"
                )
                for index, row in enumerate(relationships, start=1)
            ]

        elif category.value == "teammates":
            relationships = [
                row
                for row in relationships
                if int(row["same_games"]) >= minimum
            ]
            relationships.sort(
                key=lambda row: (
                    -int(row["same_games"]),
                    -float(row["same_win_rate"]),
                    -int(row["shared_games"]),
                    str(row["username"]).casefold(),
                )
            )
            title = f"{resolved} — Teammates"
            lines = [
                (
                    f"**{index}.** `{row['username']}` — "
                    f"**{int(row['same_games'])} game(s) together** "
                    f"• {float(row['same_win_rate']):.1f}% WR "
                    f"• {int(row['shared_games'])} total shared"
                )
                for index, row in enumerate(relationships, start=1)
            ]

        elif category.value == "best-teammates":
            relationships = [
                row
                for row in relationships
                if int(row["same_games"]) >= minimum
            ]
            relationships.sort(
                key=lambda row: (
                    -float(row["same_win_rate"]),
                    -int(row["same_games"]),
                    -int(row["same_wins"]),
                    str(row["username"]).casefold(),
                )
            )
            title = f"{resolved} — Best Teammates"
            lines = [
                (
                    f"**{index}.** `{row['username']}` — "
                    f"**{float(row['same_win_rate']):.1f}% WR** "
                    f"• {int(row['same_wins'])}W–{int(row['same_losses'])}L "
                    f"• {int(row['same_games'])} games together"
                )
                for index, row in enumerate(relationships, start=1)
            ]

        elif category.value == "best-resistance-teammates":
            relationships = [
                row
                for row in relationships
                if int(row["res_games"]) >= minimum
            ]
            relationships.sort(
                key=lambda row: (
                    -float(row["res_win_rate"]),
                    -int(row["res_games"]),
                    -int(row["res_wins"]),
                    str(row["username"]).casefold(),
                )
            )
            title = f"{resolved} — Best Resistance Teammates"
            lines = [
                (
                    f"**{index}.** `{row['username']}` — "
                    f"**{float(row['res_win_rate']):.1f}% WR** "
                    f"• {int(row['res_wins'])}W–{int(row['res_losses'])}L "
                    f"• {int(row['res_games'])} Resistance games together"
                )
                for index, row in enumerate(relationships, start=1)
            ]

        elif category.value == "best-spy-teammates":
            relationships = [
                row
                for row in relationships
                if int(row["spy_games"]) >= minimum
            ]
            relationships.sort(
                key=lambda row: (
                    -float(row["spy_win_rate"]),
                    -int(row["spy_games"]),
                    -int(row["spy_wins"]),
                    str(row["username"]).casefold(),
                )
            )
            title = f"{resolved} — Best Spy Teammates"
            lines = [
                (
                    f"**{index}.** `{row['username']}` — "
                    f"**{float(row['spy_win_rate']):.1f}% WR** "
                    f"• {int(row['spy_wins'])}W–{int(row['spy_losses'])}L "
                    f"• {int(row['spy_games'])} Spy games together"
                )
                for index, row in enumerate(relationships, start=1)
            ]

        elif category.value == "rivals":
            relationships = [
                row
                for row in relationships
                if int(row["opposite_games"]) >= minimum
            ]
            relationships.sort(
                key=lambda row: (
                    -int(row["opposite_games"]),
                    -int(row["opponent_wins"]) - int(row["opponent_losses"]),
                    str(row["username"]).casefold(),
                )
            )
            title = f"{resolved} — Rivals"
            lines = [
                (
                    f"**{index}.** `{row['username']}` — "
                    f"**{int(row['opposite_games'])} game(s) opposite** "
                    f"• {int(row['opponent_wins'])}-"
                    f"{int(row['opponent_losses'])} "
                    f"• {float(row['opponent_win_rate']):.1f}% WR"
                )
                for index, row in enumerate(relationships, start=1)
            ]

        elif category.value == "most-dominated":
            relationships = [
                row
                for row in relationships
                if int(row["opposite_games"]) >= minimum
            ]
            relationships.sort(
                key=lambda row: (
                    -float(row["opponent_win_rate"]),
                    -int(row["opponent_wins"]),
                    -int(row["opposite_games"]),
                    str(row["username"]).casefold(),
                )
            )
            title = f"{resolved} — Most Dominated"
            lines = [
                (
                    f"**{index}.** `{row['username']}` — "
                    f"**{float(row['opponent_win_rate']):.1f}% WR** "
                    f"• {int(row['opponent_wins'])}W–"
                    f"{int(row['opponent_losses'])}L "
                    f"• {int(row['opposite_games'])} opponent games"
                )
                for index, row in enumerate(relationships, start=1)
            ]

        view = SocialListView(
            title=title,
            lines=lines,
            season_name=season_display_name(season),
            minimum_games=minimum,
            page_size=int(results_per_page),
            requesting_user_id=interaction.user.id,
        )
        await interaction.followup.send(embed=view.build_embed(), view=view)


    @app_commands.command(
        name="social",
        description="Shows social statistics from archived season matches.",
    )
    @app_commands.describe(
        username="Registered season player to analyze.",
        discord_user="Linked Discord @ to look up.",
    )
    @app_commands.autocomplete(
        username=registered_username_autocomplete,
    )
    async def social(
        self,
        interaction: discord.Interaction,
        username: str | None = None,
        discord_user: discord.Member | None = None,
    ) -> None:
        if interaction.guild_id is None:
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

        requested, error = await resolve_stats_target(
            interaction,
            season,
            username=username,
            discord_user=discord_user,
        )
        if error is not None:
            await interaction.response.send_message(
                error,
                ephemeral=True,
            )
            return

        resolved = await resolve_registered_username(
            season_id=season["id"],
            requested_username=requested,
        )
        if resolved is None:
            await interaction.response.send_message(
                (
                    f"`{requested}` is not registered for "
                    f"{season_display_name(season)}."
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)

        matches = await get_season_matches(int(season["id"]))
        stats = build_player_social_stats(
            matches,
            resolved,
            minimum_shared_games=MIN_SHARED_GAMES,
        )

        if stats is None:
            await interaction.followup.send(
                (
                    f"No archived match history has been recorded yet for "
                    f"`{resolved}` in {season_display_name(season)}."
                ),
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            embed=build_social_embed(
                season_name=season_display_name(season),
                stats=stats,
            )
        )

    @app_commands.command(
        name="social-compare",
        description="Shows the archived social record between two season players.",
    )
    @app_commands.describe(
        player_one="First registered season player.",
        player_two="Second registered season player.",
    )
    @app_commands.autocomplete(
        player_one=registered_username_autocomplete,
        player_two=registered_username_autocomplete,
    )
    async def social_compare(
        self,
        interaction: discord.Interaction,
        player_one: str,
        player_two: str,
    ) -> None:
        if interaction.guild_id is None:
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

        resolved = []
        for requested in (player_one, player_two):
            value = await resolve_registered_username(
                season_id=season["id"],
                requested_username=requested.strip(),
            )
            if value is None:
                await interaction.response.send_message(
                    (
                        f"`{requested.strip()}` is not registered for "
                        f"{season_display_name(season)}."
                    ),
                    ephemeral=True,
                )
                return
            resolved.append(value)

        if resolved[0].casefold() == resolved[1].casefold():
            await interaction.response.send_message(
                "Choose two different players.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)

        matches = await get_season_matches(int(season["id"]))
        stats = build_pair_social_stats(
            matches,
            resolved[0],
            resolved[1],
        )

        if stats is None:
            await interaction.followup.send(
                (
                    f"No archived matches have been recorded with "
                    f"`{resolved[0]}` and `{resolved[1]}` together."
                ),
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            embed=build_social_compare_embed(
                season_name=season_display_name(season),
                stats=stats,
            )
        )



async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(StatsCommands(bot))
