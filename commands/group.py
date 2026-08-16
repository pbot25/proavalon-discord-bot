import asyncio
import math
import time

import discord
from discord import app_commands
from discord.ext import commands

from commands.leaderboard import (
    DEFAULT_MINIMUM_GAMES,
    LEADERBOARD_CHOICES,
    LeaderboardEntry,
    LeaderboardView,
    build_leaderboard_entry,
    sort_entries,
)
from commands.stats import build_compare_embed, get_game_size_records
from database.ratings import (
    get_cached_rating,
    get_online_usernames,
)
from database.groups import (
    CustomGroupAlreadyExistsError,
    CustomGroupPlayerAlreadyExistsError,
    CustomGroupMembershipError,
    add_custom_group_player,
    count_custom_group_players,
    create_custom_group,
    delete_custom_group,
    get_custom_group_by_name,
    get_custom_group_players,
    get_custom_groups,
    get_custom_group_for_owner,
    get_custom_group_for_member,
    remove_custom_group_player,
    remove_custom_group_member,
)
from proavalon.profile_scraper import (
    PlayerProfile,
    ProAvalonError,
    ProAvalonLoginError,
    ProAvalonUserNotFound,
)
from database.account_links import get_preferred_link_for_discord
from database.seasons import get_active_season
from utils.discord_users import resolve_discord_target
from utils.permissions import user_is_bot_admin


GROUP_PAGE_SIZE = 15
GROUP_LEADERBOARD_CACHE_SECONDS = 2 * 60 * 60


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




async def group_name_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    if interaction.guild_id is None:
        return []

    groups = await get_custom_groups(
        interaction.guild_id
    )
    normalized = current.strip().casefold()

    matches = [
        str(group["name"])
        for group in groups
        if normalized in str(group["name"]).casefold()
    ]

    return [
        app_commands.Choice(
            name=name,
            value=name,
        )
        for name in matches[:25]
    ]


async def group_player_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    if interaction.guild_id is None:
        return []

    group_name = getattr(
        interaction.namespace,
        "group",
        None,
    )

    if not isinstance(group_name, str):
        return []

    group = await get_custom_group_by_name(
        interaction.guild_id,
        group_name,
    )

    if group is None:
        return []

    players = await get_custom_group_players(
        int(group["id"])
    )
    normalized = current.strip().casefold()

    matches = [
        str(player["proavalon_username"])
        for player in players
        if normalized
        in str(player["proavalon_username"]).casefold()
    ]

    return [
        app_commands.Choice(
            name=username,
            value=username,
        )
        for username in matches[:25]
    ]


async def resolve_group(
    interaction: discord.Interaction,
    group_name: str,
):
    if interaction.guild_id is None:
        return None

    return await get_custom_group_by_name(
        interaction.guild_id,
        group_name,
    )


async def fetch_profile(
    bot: commands.Bot,
    username: str,
) -> PlayerProfile:
    profile, _ = await bot.profile_cache.get_or_fetch(
        username,
        bot.proavalon_scraper.get_profile_data,
    )

    return profile


class GroupPlayersView(discord.ui.View):
    def __init__(
        self,
        *,
        group_name: str,
        players,
        game_size: int,
        requesting_user_id: int,
    ) -> None:
        super().__init__(timeout=180)

        self.group_name = group_name
        self.players = players
        self.game_size = game_size
        self.requesting_user_id = requesting_user_id
        self.page = 0
        self.total_pages = max(
            1,
            math.ceil(
                len(players) / GROUP_PAGE_SIZE
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
        start = self.page * GROUP_PAGE_SIZE
        end = start + GROUP_PAGE_SIZE
        page_players = self.players[start:end]

        lines = [
            (
                f"`{index:>2}.` "
                + (f"<@{player['discord_user_id']}> — " if player["discord_user_id"] else "")
                + f"`{player['proavalon_username']}`"
            )
            for index, player in enumerate(
                page_players,
                start=start + 1,
            )
        ]

        description = (
            "\n".join(lines)
            if lines
            else "No players are registered in this group."
        )

        embed = discord.Embed(
            title=f"{self.group_name} — Players",
            description=description,
            color=discord.Color.blurple(),
        )

        embed.set_footer(
            text=(
                f"{len(self.players)} player(s) • "
                f"{self.game_size}p stats • "
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


class GroupLeaderboardView(LeaderboardView):
    def __init__(
        self,
        *,
        group_name: str,
        **kwargs,
    ) -> None:
        self.group_name = group_name
        super().__init__(**kwargs)

    def build_embed(self) -> discord.Embed:
        embed = super().build_embed()

        display_part = (
            embed.title.split("—", 1)[-1].strip()
            if embed.title is not None
            else "Leaderboard"
        )

        embed.title = (
            f"{self.group_name} — {display_part}"
        )

        return embed


def is_group_owner(group, discord_user_id: int) -> bool:
    return str(group["created_by_discord_user_id"]) == str(discord_user_id)


async def preferred_link_for_group_user(guild_id: int, discord_user_id: int):
    season = await get_active_season(guild_id)
    season_number = int(season["season_number"]) if season is not None else None
    return await get_preferred_link_for_discord(guild_id, discord_user_id, season_number)


def aggregate_group_profiles(profiles: list[PlayerProfile], game_size: int) -> dict:
    key=f"{game_size}p"
    ow=ol=rw=rl=sw=sl=0
    total_time=0.0
    for profile in profiles:
        a,b,c,d,e,f=get_game_size_records(profile,key)
        ow+=a; ol+=b; rw+=c; rl+=d; sw+=e; sl+=f
        total_time += profile.total_time_played_seconds or 0.0
    ratings=[]
    for profile in profiles:
        snap=get_cached_rating(profile.username)
        if snap is not None:
            ratings.append(int(snap.rating))
    gp=ow+ol
    return {
        "members":len(profiles),"games":gp,"wins":ow,"losses":ol,
        "wr":(ow/gp*100 if gp else 0.0),
        "res_games":rw+rl,"res_wins":rw,"res_losses":rl,
        "res_wr":(rw/(rw+rl)*100 if rw+rl else 0.0),
        "spy_games":sw+sl,"spy_wins":sw,"spy_losses":sl,
        "spy_wr":(sw/(sw+sl)*100 if sw+sl else 0.0),
        "total_rating":sum(ratings),"rated_members":len(ratings),
        "average_rating":(sum(ratings)/len(ratings) if ratings else None),
        "total_time_hours":total_time/3600.0,
    }


def build_group_profile_embed(group, profiles: list[PlayerProfile], failed_count: int=0) -> discord.Embed:
    stats=aggregate_group_profiles(profiles,int(group["tracked_player_count"]))
    e=discord.Embed(title=f"{group['name']} — Group Profile",color=discord.Color.blurple())
    e.description=(f"Owner: <@{group['created_by_discord_user_id']}> • **{stats['members']}** profiled member(s) • **{group['tracked_player_count']}p** statistics")
    e.add_field(name="Combined Overall",value=f"{stats['wins']}W-{stats['losses']}L • **{stats['wr']:.1f}% WR** • {stats['games']} GP",inline=False)
    e.add_field(name="Combined Resistance",value=f"{stats['res_wins']}W-{stats['res_losses']}L • **{stats['res_wr']:.1f}% WR** • {stats['res_games']} GP",inline=True)
    e.add_field(name="Combined Spy",value=f"{stats['spy_wins']}W-{stats['spy_losses']}L • **{stats['spy_wr']:.1f}% WR** • {stats['spy_games']} GP",inline=True)
    rating_text=f"**{stats['total_rating']:,}** total"
    if stats['average_rating'] is not None: rating_text += f" • **{stats['average_rating']:.0f}** avg"
    rating_text += f" • {stats['rated_members']}/{stats['members']} ratings known"
    e.add_field(name="Rating",value=rating_text,inline=False)
    e.add_field(name="Combined Time Played",value=f"{stats['total_time_hours']:,.1f} hours",inline=False)
    if failed_count: e.set_footer(text=f"Could not refresh {failed_count} member profile(s).")
    return e


class GroupCommands(
    commands.GroupCog,
    group_name="group",
    group_description="Manage and view custom ProAvalon groups.",
):
    def __init__(
        self,
        bot: commands.Bot,
    ) -> None:
        self.bot = bot

        # Cache is isolated by (guild, custom group).
        self._cached_profiles: dict[
            tuple[int, int],
            dict[str, PlayerProfile],
        ] = {}
        self._cache_refreshed_at: dict[
            tuple[int, int],
            float,
        ] = {}
        self._cache_rosters: dict[
            tuple[int, int],
            set[str],
        ] = {}
        self._refresh_locks: dict[
            tuple[int, int],
            asyncio.Lock,
        ] = {}

    def _get_lock(
        self,
        key: tuple[int, int],
    ) -> asyncio.Lock:
        lock = self._refresh_locks.get(key)

        if lock is None:
            lock = asyncio.Lock()
            self._refresh_locks[key] = lock

        return lock

    def _cache_age_seconds(
        self,
        key: tuple[int, int],
    ) -> int | None:
        refreshed_at = self._cache_refreshed_at.get(
            key
        )

        if refreshed_at is None:
            return None

        return max(
            0,
            int(time.monotonic() - refreshed_at),
        )

    def _cache_valid(
        self,
        *,
        key: tuple[int, int],
        roster: set[str],
    ) -> bool:
        refreshed_at = self._cache_refreshed_at.get(
            key
        )

        if refreshed_at is None:
            return False

        if self._cache_rosters.get(key) != roster:
            return False

        return (
            time.monotonic() - refreshed_at
            < GROUP_LEADERBOARD_CACHE_SECONDS
        )

    async def _get_profiles(
        self,
        *,
        guild_id: int,
        group_id: int,
        players,
    ) -> tuple[list[PlayerProfile], bool, int]:
        key = (
            guild_id,
            group_id,
        )
        roster = {
            str(player["proavalon_username"]).casefold()
            for player in players
        }

        if self._cache_valid(
            key=key,
            roster=roster,
        ):
            return (
                list(
                    self._cached_profiles[key].values()
                ),
                True,
                0,
            )

        async with self._get_lock(key):
            if self._cache_valid(
                key=key,
                roster=roster,
            ):
                return (
                    list(
                        self._cached_profiles[key].values()
                    ),
                    True,
                    0,
                )

            profiles: dict[
                str,
                PlayerProfile,
            ] = {}
            failed = 0

            for player in players:
                username = str(
                    player["proavalon_username"]
                )

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
                    failed += 1
                    continue

                profiles[
                    profile.username.casefold()
                ] = profile

            self._cached_profiles[key] = profiles
            self._cache_refreshed_at[key] = (
                time.monotonic()
            )
            self._cache_rosters[key] = roster

            return (
                list(profiles.values()),
                False,
                failed,
            )

    def _invalidate_group_cache(
        self,
        guild_id: int,
        group_id: int,
    ) -> None:
        key = (
            guild_id,
            group_id,
        )

        self._cached_profiles.pop(
            key,
            None,
        )
        self._cache_refreshed_at.pop(
            key,
            None,
        )
        self._cache_rosters.pop(
            key,
            None,
        )


    @app_commands.command(
        name="online-players",
        description="Shows custom-group players currently online on ProAvalon.",
    )
    @app_commands.describe(
        group="Custom group.",
    )
    @app_commands.autocomplete(
        group=group_name_autocomplete,
    )
    async def online_players(
        self,
        interaction: discord.Interaction,
        group: str,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        stored_group = await resolve_group(
            interaction,
            group,
        )

        if stored_group is None:
            await interaction.response.send_message(
                f"Could not find custom group `{group}`.",
                ephemeral=True,
            )
            return

        players = await get_custom_group_players(
            int(stored_group["id"])
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
                    f"No players from `{stored_group['name']}` "
                    "are currently online."
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
                f"{stored_group['name']} — Online Players"
            ),
            lines=lines,
            footer_label="online group player(s)",
            requesting_user_id=interaction.user.id,
        )

        await interaction.response.send_message(
            embed=view.build_embed(),
            view=view,
        )

    @app_commands.command(
        name="rank-players",
        description="Shows custom-group players under a specific ProAvalon rank.",
    )
    @app_commands.describe(
        group="Custom group.",
        rank="Rank/tier to filter this custom group by.",
    )
    @app_commands.autocomplete(
        group=group_name_autocomplete,
    )
    @app_commands.choices(
        rank=RANK_CHOICES,
    )
    async def rank_players(
        self,
        interaction: discord.Interaction,
        group: str,
        rank: app_commands.Choice[str],
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        stored_group = await resolve_group(
            interaction,
            group,
        )

        if stored_group is None:
            await interaction.response.send_message(
                f"Could not find custom group `{group}`.",
                ephemeral=True,
            )
            return

        players = await get_custom_group_players(
            int(stored_group["id"])
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
                    f"No players from `{stored_group['name']}` "
                    f"are classified as **{rank.name}**."
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
                f"{stored_group['name']} — {rank.name}"
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
        name="create",
        description="Creates your one custom ProAvalon group in this server.",
    )
    @app_commands.describe(
        name="Name of your group.",
        game_size="Tracked game size for group statistics. Defaults to 10.",
    )
    async def create(
        self,
        interaction: discord.Interaction,
        name: str,
        game_size: app_commands.Range[int, 5, 10] = 10,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("This command can only be used in a server.",ephemeral=True)
            return

        link=await preferred_link_for_group_user(interaction.guild_id,interaction.user.id)
        if link is None:
            await interaction.response.send_message("Link a ProAvalon account first with `/account link`. Your linked account becomes the first member of your group.",ephemeral=True)
            return

        try:
            stored=await create_custom_group(
                guild_id=interaction.guild_id,
                name=name,
                tracked_player_count=game_size,
                created_by_discord_user_id=interaction.user.id,
                owner_proavalon_username=str(link["proavalon_username"]),
            )
        except (CustomGroupAlreadyExistsError,CustomGroupMembershipError,ValueError) as error:
            await interaction.response.send_message(str(error),ephemeral=True);return

        await interaction.response.send_message(
            f"Created `{stored['name']}`. You are its owner and first member. Groups can contain up to **10** users.",
            ephemeral=True,
        )

    @app_commands.command(
        name="delete",
        description="Deletes a group you own; admins may delete any group.",
    )
    @app_commands.describe(
        group="Custom group to delete.",
        confirmation='Type exactly: "I understand"',
    )
    @app_commands.autocomplete(
        group=group_name_autocomplete,
    )
    async def delete(
        self,
        interaction: discord.Interaction,
        group: str,
        confirmation: str,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        if confirmation.strip() != "I understand":
            await interaction.response.send_message(
                (
                    'Deletion cancelled. Type exactly '
                    '**"I understand"** to delete a group.'
                ),
                ephemeral=True,
            )
            return

        stored_group = await resolve_group(
            interaction,
            group,
        )

        if stored_group is None:
            await interaction.response.send_message(
                f"Could not find custom group `{group}`.",
                ephemeral=True,
            )
            return

        is_owner = is_group_owner(
            stored_group,
            interaction.user.id,
        )
        is_admin = await user_is_bot_admin(interaction)

        if not is_owner and not is_admin:
            await interaction.response.send_message(
                (
                    "Only the group owner or a bot administrator "
                    "can delete this group."
                ),
                ephemeral=True,
            )
            return

        deleted = await delete_custom_group(
            interaction.guild_id,
            int(stored_group["id"]),
        )

        self._invalidate_group_cache(
            interaction.guild_id,
            int(stored_group["id"]),
        )

        await interaction.response.send_message(
            (
                f"Deleted custom group "
                f"`{deleted['name']}` and its player list."
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="invite",
        description="Adds a Discord user to your group.",
    )
    @app_commands.describe(
        group="Your custom group.",
        discord_user="Discord user to add.",
        discord_user_id="Discord user ID instead of an @ mention.",
    )
    @app_commands.autocomplete(group=group_name_autocomplete)
    async def invite(
        self,
        interaction: discord.Interaction,
        group: str,
        discord_user: discord.Member | None = None,
        discord_user_id: str | None = None,
    ) -> None:
        if interaction.guild_id is None or interaction.guild is None:
            await interaction.response.send_message("This command can only be used in a server.",ephemeral=True);return
        stored=await resolve_group(interaction,group)
        if stored is None:
            await interaction.response.send_message(f"Could not find custom group `{group}`.",ephemeral=True);return
        if not is_group_owner(stored,interaction.user.id):
            await interaction.response.send_message("Only the group owner can invite members.",ephemeral=True);return
        try:
            target_id,target_mention=resolve_discord_target(discord_user,discord_user_id)
        except ValueError as error:
            await interaction.response.send_message(str(error),ephemeral=True);return
        if target_id == interaction.user.id:
            await interaction.response.send_message("You are already the owner/member of your group.",ephemeral=True);return
        member=interaction.guild.get_member(target_id)
        if member is None:
            try: member=await interaction.guild.fetch_member(target_id)
            except (discord.NotFound,discord.Forbidden,discord.HTTPException):
                await interaction.response.send_message("That Discord user is not currently in this server.",ephemeral=True);return
        if member.bot:
            await interaction.response.send_message("Bots cannot be added to ProAvalon groups.",ephemeral=True);return
        if await count_custom_group_players(int(stored["id"])) >= 10:
            await interaction.response.send_message("This group already has the maximum of **10 members**.",ephemeral=True);return
        link=await preferred_link_for_group_user(interaction.guild_id,target_id)
        if link is None:
            await interaction.response.send_message(f"{target_mention} does not have a linked ProAvalon account in this server.",ephemeral=True,allowed_mentions=discord.AllowedMentions.none());return
        try:
            await add_custom_group_player(
                guild_id=interaction.guild_id,
                custom_group_id=int(stored["id"]),
                discord_user_id=target_id,
                proavalon_username=str(link["proavalon_username"]),
                added_by_discord_user_id=interaction.user.id,
            )
        except (CustomGroupMembershipError,CustomGroupPlayerAlreadyExistsError,ValueError) as error:
            await interaction.response.send_message(str(error),ephemeral=True);return
        self._invalidate_group_cache(interaction.guild_id,int(stored["id"]))
        await interaction.response.send_message(
            f"Added {target_mention} (`{link['proavalon_username']}`) to `{stored['name']}`.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(
        name="kick",
        description="Removes a Discord user from your group.",
    )
    @app_commands.describe(
        group="Your custom group.",
        discord_user="Discord user to remove.",
        discord_user_id="Discord user ID instead of an @ mention.",
    )
    @app_commands.autocomplete(group=group_name_autocomplete)
    async def kick(
        self,
        interaction: discord.Interaction,
        group: str,
        discord_user: discord.Member | None = None,
        discord_user_id: str | None = None,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("This command can only be used in a server.",ephemeral=True);return
        stored=await resolve_group(interaction,group)
        if stored is None:
            await interaction.response.send_message(f"Could not find custom group `{group}`.",ephemeral=True);return
        if not is_group_owner(stored,interaction.user.id):
            await interaction.response.send_message("Only the group owner can kick members.",ephemeral=True);return
        try:
            target_id,target_mention=resolve_discord_target(discord_user,discord_user_id)
        except ValueError as error:
            await interaction.response.send_message(str(error),ephemeral=True);return
        if str(target_id)==str(stored["created_by_discord_user_id"]):
            await interaction.response.send_message("The group owner cannot be kicked. Delete the group instead.",ephemeral=True);return
        removed=await remove_custom_group_member(int(stored["id"]),target_id)
        if removed is None:
            await interaction.response.send_message("That user is not a member of this group.",ephemeral=True);return
        self._invalidate_group_cache(interaction.guild_id,int(stored["id"]))
        await interaction.response.send_message(
            f"Removed {target_mention} (`{removed['proavalon_username']}`) from `{stored['name']}`.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(
        name="list",
        description="Lists all custom groups in this server.",
    )
    async def list_groups(
        self,
        interaction: discord.Interaction,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        groups = await get_custom_groups(
            interaction.guild_id
        )

        if not groups:
            await interaction.response.send_message(
                "No custom groups exist in this server."
            )
            return

        lines = []

        for group in groups:
            count = await count_custom_group_players(
                int(group["id"])
            )

            lines.append(
                (
                    f"**{group['name']}** — "
                    f"{count}/10 member(s) • "
                    f"{group['tracked_player_count']}p • "
                    f"Owner: <@{group['created_by_discord_user_id']}>"
                )
            )

        embed = discord.Embed(
            title="Custom Groups",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )

        await interaction.response.send_message(
            embed=embed
        )

    @app_commands.command(
        name="info",
        description="Shows information about a custom group.",
    )
    @app_commands.describe(
        group="Custom group.",
    )
    @app_commands.autocomplete(
        group=group_name_autocomplete,
    )
    async def info(
        self,
        interaction: discord.Interaction,
        group: str,
    ) -> None:
        stored_group = await resolve_group(
            interaction,
            group,
        )

        if stored_group is None:
            await interaction.response.send_message(
                f"Could not find custom group `{group}`.",
                ephemeral=True,
            )
            return

        count = await count_custom_group_players(
            int(stored_group["id"])
        )

        embed = discord.Embed(
            title=f"Custom Group — {stored_group['name']}",
            color=discord.Color.blurple(),
        )

        embed.add_field(
            name="Tracked game size",
            value=(
                f"{stored_group['tracked_player_count']} players"
            ),
            inline=True,
        )
        embed.add_field(
            name="Registered players",
            value=f"{count:,}/10",
            inline=True,
        )
        embed.add_field(
            name="Owner",
            value=f"<@{stored_group['created_by_discord_user_id']}>",
            inline=True,
        )
        embed.add_field(
            name="Created",
            value=str(stored_group["created_at"])[:10],
            inline=True,
        )

        await interaction.response.send_message(
            embed=embed
        )

    @app_commands.command(
        name="players",
        description="Shows all players in a custom group.",
    )
    @app_commands.describe(
        group="Custom group.",
    )
    @app_commands.autocomplete(
        group=group_name_autocomplete,
    )
    async def players(
        self,
        interaction: discord.Interaction,
        group: str,
    ) -> None:
        stored_group = await resolve_group(
            interaction,
            group,
        )

        if stored_group is None:
            await interaction.response.send_message(
                f"Could not find custom group `{group}`.",
                ephemeral=True,
            )
            return

        players = await get_custom_group_players(
            int(stored_group["id"])
        )

        view = GroupPlayersView(
            group_name=str(
                stored_group["name"]
            ),
            players=players,
            game_size=int(
                stored_group["tracked_player_count"]
            ),
            requesting_user_id=interaction.user.id,
        )

        await interaction.response.send_message(
            embed=view.build_embed(),
            view=view,
        )

    @app_commands.command(
        name="profile",
        description="Shows the combined statistics of a custom group.",
    )
    @app_commands.describe(group="Custom group.")
    @app_commands.autocomplete(group=group_name_autocomplete)
    async def profile(self, interaction: discord.Interaction, group: str) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("This command can only be used in a server.",ephemeral=True);return
        stored=await resolve_group(interaction,group)
        if stored is None:
            await interaction.response.send_message(f"Could not find custom group `{group}`.",ephemeral=True);return
        players=await get_custom_group_players(int(stored["id"]))
        if not players:
            await interaction.response.send_message("That group has no members.",ephemeral=True);return
        await interaction.response.defer(thinking=True)
        profiles,_,failed=await self._get_profiles(guild_id=interaction.guild_id,group_id=int(stored["id"]),players=players)
        await interaction.followup.send(embed=build_group_profile_embed(stored,profiles,failed))

    @app_commands.command(
        name="compare-groups",
        description="Compares the combined statistics and ratings of two groups.",
    )
    @app_commands.describe(group_one="First group.",group_two="Second group.")
    @app_commands.autocomplete(group_one=group_name_autocomplete,group_two=group_name_autocomplete)
    async def compare_groups(self,interaction:discord.Interaction,group_one:str,group_two:str)->None:
        if interaction.guild_id is None:
            await interaction.response.send_message("This command can only be used in a server.",ephemeral=True);return
        one=await resolve_group(interaction,group_one);two=await resolve_group(interaction,group_two)
        if one is None or two is None:
            await interaction.response.send_message("One or both groups could not be found.",ephemeral=True);return
        if int(one["id"])==int(two["id"]):
            await interaction.response.send_message("Choose two different groups.",ephemeral=True);return
        if int(one["tracked_player_count"])!=int(two["tracked_player_count"]):
            await interaction.response.send_message("These groups use different tracked game sizes, so their combined game statistics are not directly comparable.",ephemeral=True);return
        await interaction.response.defer(thinking=True)
        p1=await get_custom_group_players(int(one["id"]));p2=await get_custom_group_players(int(two["id"]))
        prof1,_,f1=await self._get_profiles(guild_id=interaction.guild_id,group_id=int(one["id"]),players=p1)
        prof2,_,f2=await self._get_profiles(guild_id=interaction.guild_id,group_id=int(two["id"]),players=p2)
        s1=aggregate_group_profiles(prof1,int(one["tracked_player_count"]));s2=aggregate_group_profiles(prof2,int(two["tracked_player_count"]))
        def side(name,s):
            avg="N/A" if s['average_rating'] is None else f"{s['average_rating']:.0f}"
            return (f"**Members:** {s['members']}\n**Total rating:** {s['total_rating']:,}\n**Average rating:** {avg}\n"
                    f"**Overall:** {s['wins']}W-{s['losses']}L • {s['wr']:.1f}%\n"
                    f"**Resistance:** {s['res_wr']:.1f}% ({s['res_games']} GP)\n**Spy:** {s['spy_wr']:.1f}% ({s['spy_games']} GP)")
        e=discord.Embed(title=f"{one['name']} vs {two['name']}",description=f"Combined {one['tracked_player_count']}p group statistics",color=discord.Color.blurple())
        e.add_field(name=str(one['name']),value=side(str(one['name']),s1),inline=True)
        e.add_field(name=str(two['name']),value=side(str(two['name']),s2),inline=True)
        e.set_footer(text=f"Profile refresh failures: {f1+f2}")
        await interaction.followup.send(embed=e)

    @app_commands.command(
        name="compare",
        description="Compares two players inside a custom group.",
    )
    @app_commands.describe(
        group="Custom group.",
        player_one="Player 1.",
        player_two="Player 2.",
    )
    @app_commands.autocomplete(
        group=group_name_autocomplete,
        player_one=group_player_autocomplete,
        player_two=group_player_autocomplete,
    )
    async def compare(
        self,
        interaction: discord.Interaction,
        group: str,
        player_one: str,
        player_two: str,
    ) -> None:
        stored_group = await resolve_group(
            interaction,
            group,
        )

        if stored_group is None:
            await interaction.response.send_message(
                f"Could not find custom group `{group}`.",
                ephemeral=True,
            )
            return

        players = await get_custom_group_players(
            int(stored_group["id"])
        )

        registered = {
            str(player["proavalon_username"]).casefold():
                str(player["proavalon_username"])
            for player in players
        }

        requested = [
            player_one.strip(),
            player_two.strip(),
        ]

        resolved: list[str] = []

        for username in requested:
            stored = registered.get(
                username.casefold()
            )

            if stored is None:
                await interaction.response.send_message(
                    (
                        f"`{username}` is not registered in "
                        f"`{stored_group['name']}`."
                    ),
                    ephemeral=True,
                )
                return

            resolved.append(stored)

        if resolved[0].casefold() == resolved[1].casefold():
            await interaction.response.send_message(
                "Choose two different players to compare.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(
            thinking=True
        )

        try:
            profiles = [
                await fetch_profile(
                    self.bot,
                    username,
                )
                for username in resolved
            ]
        except (
            ProAvalonUserNotFound,
            ProAvalonLoginError,
            ProAvalonError,
            asyncio.TimeoutError,
            ValueError,
        ) as error:
            await interaction.followup.send(
                f"Could not compare those profiles: {error}",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            embed=build_compare_embed(
                profiles=profiles,
                tracked_player_count=int(
                    stored_group["tracked_player_count"]
                ),
            )
        )

    @app_commands.command(
        name="leaderboard",
        description="Shows a leaderboard for a custom group.",
    )
    @app_commands.describe(
        group="Custom group.",
        statistic="Statistic to rank.",
        players_shown=(
            "Optional maximum number of players to display."
        ),
        minimum_games=(
            "Optional minimum games required to qualify."
        ),
    )
    @app_commands.autocomplete(
        group=group_name_autocomplete,
    )
    @app_commands.choices(
        statistic=LEADERBOARD_CHOICES,
    )
    async def leaderboard(
        self,
        interaction: discord.Interaction,
        group: str,
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

        stored_group = await resolve_group(
            interaction,
            group,
        )

        if stored_group is None:
            await interaction.response.send_message(
                f"Could not find custom group `{group}`.",
                ephemeral=True,
            )
            return

        players = await get_custom_group_players(
            int(stored_group["id"])
        )

        if not players:
            await interaction.response.send_message(
                (
                    f"`{stored_group['name']}` has no "
                    "registered players."
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer(
            thinking=True
        )

        profiles, cache_hit, failed_count = (
            await self._get_profiles(
                guild_id=interaction.guild_id,
                group_id=int(
                    stored_group["id"]
                ),
                players=players,
            )
        )

        metric = statistic.value
        game_size_key = (
            f"{stored_group['tracked_player_count']}p"
        )

        entries: list[LeaderboardEntry] = [
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

        entries = sort_entries(
            entries,
            metric=metric,
        )

        total_before_limit = len(entries)

        if players_shown is not None:
            entries = entries[:players_shown]

        key = (
            interaction.guild_id,
            int(stored_group["id"]),
        )

        view = GroupLeaderboardView(
            group_name=str(stored_group["name"]),
            entries=entries,
            season_number=0,
            metric=metric,
            minimum_games=minimum_games,
            requested_limit=players_shown,
            total_qualifying_before_limit=(
                total_before_limit
            ),
            refreshed_count=(
                0
                if cache_hit
                else len(profiles)
            ),
            failed_count=failed_count,
            cache_hit=cache_hit,
            cache_age_seconds=self._cache_age_seconds(
                key
            ),
            requesting_user_id=interaction.user.id,
        )

        await interaction.followup.send(
            embed=view.build_embed(),
            view=view,
        )


async def setup(
    bot: commands.Bot,
) -> None:
    await bot.add_cog(
        GroupCommands(bot)
    )