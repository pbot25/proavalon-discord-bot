from __future__ import annotations

import asyncio
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from commands.leaderboard import get_metric_record
from database.discord_roles import (
    ALL_ROLE_KEYS,
    AWARD_KEYS,
    MINIMUM_AWARD_KEYS,
    RANK_KEYS,
    clear_assignment,
    clear_role_config,
    get_active_season_linked_users,
    get_assignment,
    get_award_assignments,
    get_all_award_minimum_games,
    get_award_minimum_games,
    get_known_ratings,
    get_rank_assignments,
    get_role_configs,
    set_assignment,
    set_award_minimum_games,
    set_role_config,
    get_crit_award_minimum_votes,
    set_crit_award_minimum_votes,
)
from database.seasons import get_active_season
from database.voting_metrics import get_season_voting_metrics
from proavalon.profile_scraper import (
    ProAvalonError,
    ProAvalonLoginError,
    ProAvalonUserNotFound,
)
from utils.permissions import bot_admin_only


ROLE_TARGET_NAMES = {
    "rank_unranked": "Unranked",
    "rank_iron": "Iron",
    "rank_bronze": "Bronze",
    "rank_silver": "Silver",
    "rank_gold": "Gold",
    "rank_platinum": "Platinum",
    "rank_diamond": "Diamond",
    "rank_champion": "Champion",
    "award_best_resistance": "Best Resistance WR",
    "award_best_spy": "Best Spy WR",
    "award_highest_rating": "Highest Rating",
    "award_lowest_rating": "Lowest Rating",
    "award_most_games": "Most Games Played",
    "award_highest_crit_accuracy": "Highest Crit Voting Accuracy",
    "award_lowest_crit_accuracy": "Lowest Crit Voting Accuracy",
}

ROLE_TARGET_CHOICES = [
    app_commands.Choice(name=ROLE_TARGET_NAMES[key], value=key)
    for key in ALL_ROLE_KEYS
]

AWARD_TARGET_CHOICES = [
    app_commands.Choice(name=ROLE_TARGET_NAMES[key], value=key)
    for key in MINIMUM_AWARD_KEYS
]

PROVISIONAL_GAMES = 20

VALID_BRACKETS = {
    "unranked", "iron", "bronze", "silver", "gold",
    "platinum", "diamond", "champion",
}


class RoleSyncRateLimited(RuntimeError):
    """Stop an entire role reconciliation after Discord rejects one write."""


class RoleSyncResult:
    def __init__(self) -> None:
        self.added = 0
        self.removed = 0
        self.members = 0
        self.profile_failures = 0
        self.errors: list[str] = []
        self.award_winners: dict[str, list[str]] = {}
        # Every successful Discord role mutation performed by this sync.
        # Used by /roles sync and automatic match-history updates.
        self.changes: list[dict[str, object]] = []
        # Award-holder set changes (including ties/transfers).
        self.award_transitions: list[dict[str, object]] = []


def normalize_bracket(value: str | None) -> str:
    if not value:
        return "unranked"
    normalized = str(value).strip().casefold()
    return normalized if normalized in VALID_BRACKETS else "unranked"


async def fetch_member(guild: discord.Guild, user_id: int):
    member = guild.get_member(user_id)
    if member is not None:
        return member
    try:
        return await guild.fetch_member(user_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None


def manageable_role(guild: discord.Guild, role_id: int) -> discord.Role | None:
    role = guild.get_role(role_id)
    if role is None or role.is_default() or role.managed:
        return None
    me = guild.me
    if me is None or role.position >= me.top_role.position:
        return None
    return role


class DiscordRoleCommands(
    commands.GroupCog,
    group_name="roles",
    group_description="Configure automatic ProAvalon Discord roles.",
):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._sync_lock = asyncio.Lock()
        self._role_sync_blocked_until = 0.0
        self.auto_sync.start()

    def cog_unload(self) -> None:
        self.auto_sync.cancel()

    async def _profile(self, username: str):
        return await self.bot.profile_cache.get_or_fetch(
            username,
            self.bot.proavalon_scraper.get_profile_data,
        )

    async def _remove_role_from_member(
        self,
        member: discord.Member | None,
        role: discord.Role | None,
        result: RoleSyncResult,
    ) -> None:
        if member is None or role is None or role not in member.roles:
            return
        try:
            await member.remove_roles(role, reason="ProAvalon automatic role sync")
            result.removed += 1
            result.changes.append({
                "action": "removed",
                "user_id": member.id,
                "role_id": role.id,
                "role_name": role.name,
            })
        except (discord.Forbidden, discord.HTTPException) as error:
            result.errors.append(f"Could not remove {role.name}: {error}")
            if getattr(error, "status", None) == 429:
                # Cloudflare 1015 responses do not carry Discord's normal
                # rate-limit headers, so discord.py cannot safely wait and
                # retry them. Continuing through every member/role turns one
                # rejection into a request storm that can also block commands
                # and match-history messages from this Railway service.
                self._role_sync_blocked_until = time.monotonic() + 3600
                raise RoleSyncRateLimited(
                    "Discord rejected a role update; role syncing is paused "
                    "for one hour."
                ) from error

    async def _add_role_to_member(
        self,
        member: discord.Member | None,
        role: discord.Role | None,
        result: RoleSyncResult,
    ) -> None:
        if member is None or role is None or role in member.roles:
            return
        try:
            await member.add_roles(role, reason="ProAvalon automatic role sync")
            result.added += 1
            result.changes.append({
                "action": "added",
                "user_id": member.id,
                "role_id": role.id,
                "role_name": role.name,
            })
        except (discord.Forbidden, discord.HTTPException) as error:
            result.errors.append(f"Could not add {role.name}: {error}")
            if getattr(error, "status", None) == 429:
                self._role_sync_blocked_until = time.monotonic() + 3600
                raise RoleSyncRateLimited(
                    "Discord rejected a role update; role syncing is paused "
                    "for one hour."
                ) from error

    async def _set_award_holders(
        self,
        guild: discord.Guild,
        role_key: str,
        role: discord.Role | None,
        winners: list[tuple[int, str]],
        linked_members: dict[int, discord.Member],
        result: RoleSyncResult,
    ) -> None:
        """Fully reconcile an award role to every current winner.

        This intentionally does not trust only the assignment-tracking table.
        If a role was left on someone by an older bug/manual edit, the sync
        removes it from every known non-winner as well.
        """
        desired_ids = {user_id for user_id, _ in winners}
        assignments = await get_award_assignments(guild.id, role_key)

        # Assignment rows are the canonical previous winner set for
        # announcements. Discord's member/role cache is also scanned below for
        # cleanup, but stale Discord state must not cause the same title
        # transfer to be announced again on a later match.
        tracked_previous_ids = {
            int(assignment["discord_user_id"])
            for assignment in assignments
        }

        # Build the real current-holder set from three sources:
        # 1) tracked assignment rows, 2) every linked season member (which we
        # explicitly fetched), and 3) Discord's cached role member list.
        current_holders: dict[int, discord.Member | None] = {}
        for assignment in assignments:
            old_id = int(assignment["discord_user_id"])
            current_holders[old_id] = linked_members.get(old_id)

        if role is not None:
            for user_id, member in linked_members.items():
                if role in member.roles:
                    current_holders[user_id] = member
            for member in role.members:
                current_holders[member.id] = member

        # Remove the configured award role from EVERY known holder who is no
        # longer a winner, including stale holders not represented in DB rows.
        if role is not None:
            for user_id, member in list(current_holders.items()):
                if user_id in desired_ids:
                    continue
                if member is None:
                    member = await fetch_member(guild, user_id)
                await self._remove_role_from_member(member, role, result)

        # Rebuild assignment tracking from the actual desired winner set.
        for assignment in assignments:
            await clear_assignment(guild.id, str(assignment["assignment_key"]))

        if role is None:
            return

        applied_names: list[str] = []
        for winner_id, winner_username in winners:
            winner = linked_members.get(winner_id)
            if winner is None:
                winner = await fetch_member(guild, winner_id)
            if winner is None:
                continue

            await self._add_role_to_member(winner, role, result)
            await set_assignment(
                guild.id,
                f"{role_key}:{winner_id}",
                winner_id,
                role.id,
            )
            applied_names.append(winner_username)

        if applied_names:
            result.award_winners[role_key] = applied_names

        if tracked_previous_ids != desired_ids:
            result.award_transitions.append({
                "role_key": role_key,
                "role_id": role.id,
                "previous_user_ids": sorted(tracked_previous_ids),
                "new_user_ids": sorted(desired_ids),
            })

    async def sync_guild(self, guild: discord.Guild) -> RoleSyncResult:
        """Serialize role writes and honor the global 429 circuit breaker."""
        result = RoleSyncResult()
        remaining = self._role_sync_blocked_until - time.monotonic()
        if remaining > 0:
            result.errors.append(
                f"Role sync paused after a Discord rate limit "
                f"({int(remaining) + 1}s remaining)."
            )
            return result

        # The scheduled sync and post-match sync used to run concurrently,
        # multiplying member and role requests. Queue one reconciliation at a
        # time instead.
        async with self._sync_lock:
            remaining = self._role_sync_blocked_until - time.monotonic()
            if remaining > 0:
                result.errors.append(
                    f"Role sync paused after a Discord rate limit "
                    f"({int(remaining) + 1}s remaining)."
                )
                return result
            return await self._sync_guild_unlocked(guild)

    async def _sync_guild_unlocked(self, guild: discord.Guild) -> RoleSyncResult:
        result = RoleSyncResult()
        configs = await get_role_configs(guild.id)
        if not configs:
            return result

        me = guild.me
        if me is None or not me.guild_permissions.manage_roles:
            result.errors.append(
                "The bot does not have Discord's Manage Roles permission."
            )
            return result

        season = await get_active_season(guild.id)
        if season is None:
            result.errors.append("No active community season.")
            return result

        links = await get_active_season_linked_users(
            guild.id,
            int(season["season_number"]),
        )
        linked = [
            (int(row["discord_user_id"]), str(row["proavalon_username"]))
            for row in links
            if row["discord_user_id"] is not None
        ]
        result.members = len(linked)

        usernames = [username for _, username in linked]
        ratings = await get_known_ratings(usernames)

        linked_members: dict[int, discord.Member] = {}
        for user_id, _ in linked:
            member = await fetch_member(guild, user_id)
            if member is not None:
                linked_members[user_id] = member
        rank_role_ids = {
            key: configs[key]
            for key in RANK_KEYS
            if key in configs
        }
        configured_rank_roles = {
            rid: manageable_role(guild, rid)
            for rid in rank_role_ids.values()
        }

        # Clean bot-managed rank roles from anyone who is no longer linked to
        # this active season. This is intentionally authoritative: once a role
        # is configured as a ProAvalon rank role, the bot owns its membership.
        current_ids = {user_id for user_id, _ in linked}
        for role in configured_rank_roles.values():
            if role is None:
                continue
            for member in list(role.members):
                if member.id not in current_ids:
                    await self._remove_role_from_member(member, role, result)

        # Clean rank assignment rows for people no longer linked to this season.
        for assignment in await get_rank_assignments(guild.id):
            assignment_key = str(assignment["assignment_key"])
            old_user_id = int(assignment["discord_user_id"])
            if old_user_id in current_ids:
                continue
            old_role = guild.get_role(int(assignment["role_id"]))
            await self._remove_role_from_member(
                await fetch_member(guild, old_user_id), old_role, result
            )
            await clear_assignment(guild.id, assignment_key)

        profiles: dict[str, object] = {}

        # Rank roles: exactly one configured rank role per linked player.
        for user_id, username in linked:
            member = linked_members.get(user_id)
            if member is None:
                continue

            rating_data = ratings.get(username.casefold())
            if rating_data is not None:
                bracket = normalize_bracket(rating_data[1])
            else:
                try:
                    profile, _ = await self._profile(username)
                    profiles[username.casefold()] = profile
                    bracket = normalize_bracket(
                        getattr(profile, "rating_bracket", None)
                    )
                except (
                    ProAvalonUserNotFound,
                    ProAvalonLoginError,
                    ProAvalonError,
                    asyncio.TimeoutError,
                    ValueError,
                ):
                    result.profile_failures += 1
                    bracket = "unranked"

            desired_key = f"rank_{bracket}"
            desired_role_id = configs.get(desired_key)
            desired_role = (
                manageable_role(guild, desired_role_id)
                if desired_role_id is not None
                else None
            )

            # Remove any other configured rank roles currently on this member.
            for role_id, role in configured_rank_roles.items():
                if role is not None and role != desired_role and role in member.roles:
                    await self._remove_role_from_member(member, role, result)

            assignment_key = f"rank:{user_id}"
            previous = await get_assignment(guild.id, assignment_key)
            if previous is not None:
                previous_role_id = int(previous["role_id"])
                if desired_role is None or previous_role_id != desired_role.id:
                    previous_role = guild.get_role(previous_role_id)
                    await self._remove_role_from_member(member, previous_role, result)
                    await clear_assignment(guild.id, assignment_key)

            if desired_role is not None:
                await self._add_role_to_member(member, desired_role, result)
                await set_assignment(
                    guild.id, assignment_key, user_id, desired_role.id
                )

        needs_award_profiles = any(
            key in configs
            for key in (
                "award_best_resistance",
                "award_best_spy",
                "award_highest_rating",
                "award_lowest_rating",
                "award_most_games",
                "award_highest_crit_accuracy",
                "award_lowest_crit_accuracy",
            )
        )

        if needs_award_profiles:
            for _, username in linked:
                key = username.casefold()
                if key in profiles:
                    continue
                try:
                    profile, _ = await self._profile(username)
                    profiles[key] = profile
                except (
                    ProAvalonUserNotFound,
                    ProAvalonLoginError,
                    ProAvalonError,
                    asyncio.TimeoutError,
                    ValueError,
                ):
                    result.profile_failures += 1

        award_minimums = await get_all_award_minimum_games(guild.id)
        game_size_key = f"{int(season['tracked_player_count'])}p"

        # Build candidate records once.
        candidate_by_username: dict[str, dict] = {}
        for user_id, username in linked:
            profile = profiles.get(username.casefold())
            if profile is None:
                continue
            res_w, res_l = get_metric_record(profile, game_size_key, "resistance")
            spy_w, spy_l = get_metric_record(profile, game_size_key, "spy")
            rating_data = ratings.get(username.casefold())
            rating = rating_data[0] if rating_data is not None else -1
            candidate_by_username[username.casefold()] = {
                "user_id": user_id,
                "username": username,
                "res_games": res_w + res_l,
                "res_wr": (res_w / (res_w + res_l) * 100) if res_w + res_l else 0.0,
                "spy_games": spy_w + spy_l,
                "spy_wr": (spy_w / (spy_w + spy_l) * 100) if spy_w + spy_l else 0.0,
                "overall_games": res_w + res_l + spy_w + spy_l,
                "rating": rating,
            }

        # Most Games Played is based on the same tracked 10-player
        # season record used for the other profile-derived season metrics.
        # A player must have played at least one game; all ties share the role.
        played_candidates = [
            item
            for item in candidate_by_username.values()
            if item["overall_games"] >= 1
        ]
        most_games_winners: list[dict] = []
        if played_candidates:
            most_games = max(
                item["overall_games"]
                for item in played_candidates
            )
            most_games_winners = [
                item
                for item in played_candidates
                if item["overall_games"] == most_games
            ]

        most_games_role_id = configs.get("award_most_games")
        most_games_role = (
            manageable_role(guild, most_games_role_id)
            if most_games_role_id
            else None
        )
        await self._set_award_holders(
            guild,
            "award_most_games",
            most_games_role,
            [
                (int(winner["user_id"]), str(winner["username"]))
                for winner in most_games_winners
            ],
            linked_members,
            result,
        )

        def best_alignment(
            metric: str,
            minimum_games: int,
        ) -> list[dict]:
            games_key = f"{metric}_games"
            wr_key = f"{metric}_wr"
            eligible = [
                item
                for item in candidate_by_username.values()
                if item[games_key] >= minimum_games
            ]
            if not eligible:
                return []

            best_wr = max(item[wr_key] for item in eligible)
            return [
                item
                for item in eligible
                if item[wr_key] == best_wr
            ]

        # Resistance/Spy awards count games in that alignment specifically.
        best_res = best_alignment(
            "res",
            award_minimums["award_best_resistance"],
        )
        best_spy = best_alignment(
            "spy",
            award_minimums["award_best_spy"],
        )

        for role_key, winners in (
            ("award_best_resistance", best_res),
            ("award_best_spy", best_spy),
        ):
            role_id = configs.get(role_key)
            role = manageable_role(guild, role_id) if role_id else None
            await self._set_award_holders(
                guild,
                role_key,
                role,
                [
                    (int(winner["user_id"]), str(winner["username"]))
                    for winner in winners
                ],
                linked_members,
                result,
            )

        # Crit Voting Accuracy awards use archived eligible crit VOTES,
        # not games. Default qualification is 15 votes.
        if (
            "award_highest_crit_accuracy" in configs
            or "award_lowest_crit_accuracy" in configs
        ):
            voting_metrics = await get_season_voting_metrics(int(season["id"]))
            crit_candidates = []
            for user_id, username in linked:
                item = voting_metrics.get(username.casefold())
                if item is None or item.overall.accuracy is None:
                    continue
                crit_candidates.append({
                    "user_id": user_id,
                    "username": username,
                    "votes": item.overall.eligible_votes,
                    "accuracy": item.overall.accuracy,
                })

            for role_key, choose_highest in (
                ("award_highest_crit_accuracy", True),
                ("award_lowest_crit_accuracy", False),
            ):
                minimum_votes = await get_crit_award_minimum_votes(
                    guild.id, role_key
                )
                eligible = [
                    item for item in crit_candidates
                    if item["votes"] >= minimum_votes
                ]
                winners = []
                if eligible:
                    target = (
                        max(item["accuracy"] for item in eligible)
                        if choose_highest
                        else min(item["accuracy"] for item in eligible)
                    )
                    winners = [
                        item for item in eligible
                        if abs(item["accuracy"] - target) < 1e-9
                    ]
                role_id = configs.get(role_key)
                role = manageable_role(guild, role_id) if role_id else None
                await self._set_award_holders(
                    guild,
                    role_key,
                    role,
                    [
                        (int(w["user_id"]), str(w["username"]))
                        for w in winners
                    ],
                    linked_members,
                    result,
                )

        rated_candidates: list[tuple[int, int, str, int]] = []
        for user_id, username in linked:
            rating_data = ratings.get(username.casefold())
            candidate = candidate_by_username.get(username.casefold())
            if rating_data is not None and candidate is not None:
                rated_candidates.append(
                    (
                        int(rating_data[0]),
                        user_id,
                        username,
                        int(candidate["overall_games"]),
                    )
                )

        # Rating awards use total tracked games. Highest and Lowest Rating may
        # have independent minimum-game requirements.
        highest_eligible = [
            item
            for item in rated_candidates
            if item[3] >= award_minimums["award_highest_rating"]
        ]
        lowest_eligible = [
            item
            for item in rated_candidates
            if item[3] >= award_minimums["award_lowest_rating"]
        ]

        highest_winners: list[tuple[int, int, str, int]] = []
        lowest_winners: list[tuple[int, int, str, int]] = []
        if highest_eligible:
            highest_rating = max(item[0] for item in highest_eligible)
            highest_winners = [
                item for item in highest_eligible if item[0] == highest_rating
            ]
        if lowest_eligible:
            lowest_rating = min(item[0] for item in lowest_eligible)
            lowest_winners = [
                item for item in lowest_eligible if item[0] == lowest_rating
            ]

        for role_key, winners in (
            ("award_highest_rating", highest_winners),
            ("award_lowest_rating", lowest_winners),
        ):
            role_id = configs.get(role_key)
            role = manageable_role(guild, role_id) if role_id else None
            await self._set_award_holders(
                guild,
                role_key,
                role,
                [(winner[1], winner[2]) for winner in winners],
                linked_members,
                result,
            )

        # Flag configured roles the bot cannot manage.
        for key, role_id in configs.items():
            if manageable_role(guild, role_id) is None:
                role = guild.get_role(role_id)
                role_name = role.name if role is not None else f"ID {role_id}"
                result.errors.append(
                    f"Cannot manage configured role {role_name} ({ROLE_TARGET_NAMES.get(key, key)})."
                )

        return result

    @tasks.loop(minutes=15)
    async def auto_sync(self) -> None:
        for guild in self.bot.guilds:
            try:
                result = await self.sync_guild(guild)
                if result.errors:
                    print(
                        f"Discord role sync warnings for guild {guild.id}: "
                        + " | ".join(result.errors)
                    )
            except Exception as error:
                print(
                    f"Discord role sync failed for guild {guild.id}: "
                    f"{type(error).__name__}: {error}"
                )

    @auto_sync.before_loop
    async def before_auto_sync(self) -> None:
        await self.bot.wait_until_ready()
        await asyncio.sleep(10)

    @app_commands.command(
        name="set",
        description="Assigns a Discord role to a ProAvalon rank or season award.",
    )
    @app_commands.describe(
        target="Rank/award this Discord role represents.",
        role="Discord role the bot should manage.",
        minimum_matches=(
            "Special awards only. Minimum qualifying games; defaults to "
            "ProAvalon's 20-game provisional threshold."
        ),
    )
    @app_commands.choices(target=ROLE_TARGET_CHOICES)
    @bot_admin_only()
    async def set_role(
        self,
        interaction: discord.Interaction,
        target: app_commands.Choice[str],
        role: discord.Role,
        minimum_matches: app_commands.Range[int, 1, 999] = PROVISIONAL_GAMES,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return

        me = interaction.guild.me
        if me is None or not me.guild_permissions.manage_roles:
            await interaction.response.send_message(
                "Give the bot the **Manage Roles** permission first.",
                ephemeral=True,
            )
            return
        if role.is_default() or role.managed:
            await interaction.response.send_message(
                "That role cannot be managed by the bot.", ephemeral=True
            )
            return
        if role.position >= me.top_role.position:
            await interaction.response.send_message(
                "Move the bot's Discord role **above** that role in the server role list first.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True, ephemeral=True)

        try:
            await set_role_config(interaction.guild.id, target.value, role.id)
            if target.value in MINIMUM_AWARD_KEYS:
                await set_award_minimum_games(
                    interaction.guild.id,
                    target.value,
                    minimum_matches,
                )
        except ValueError as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return

        # Applying a role configuration should take effect immediately rather
        # than waiting for the next match or 15-minute backup sync.
        result = await self.sync_guild(interaction.guild)

        message = f"{role.mention} is now the **{target.name}** role."
        if target.value in MINIMUM_AWARD_KEYS:
            unit = (
                "Resistance games"
                if target.value == "award_best_resistance"
                else "Spy games"
                if target.value == "award_best_spy"
                else "total games"
            )
            message += (
                f"\nMinimum requirement: **{minimum_matches} {unit}**."
            )
        message += (
            f"\n\nRoles updated immediately: "
            f"**{result.added} added**, **{result.removed} removed**."
        )

        await interaction.followup.send(message, ephemeral=True)

    @app_commands.command(
        name="clear",
        description="Stops managing one configured rank/award role.",
    )
    @app_commands.choices(target=ROLE_TARGET_CHOICES)
    @bot_admin_only()
    async def clear_role(
        self,
        interaction: discord.Interaction,
        target: app_commands.Choice[str],
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        removed = await clear_role_config(interaction.guild_id, target.value)
        await interaction.response.send_message(
            (
                f"Cleared the **{target.name}** role configuration."
                if removed is not None
                else f"No role was configured for **{target.name}**."
            ),
            ephemeral=True,
        )

    async def _set_minimum_matches_and_sync(
        self,
        interaction: discord.Interaction,
        target: app_commands.Choice[str],
        games: int,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True, ephemeral=True)
        await set_award_minimum_games(
            interaction.guild.id,
            target.value,
            games,
        )

        # Important: immediately recalculate all holders. Raising the
        # requirement therefore removes the role from anyone who no longer
        # qualifies before this command returns.
        result = await self.sync_guild(interaction.guild)

        unit = (
            "Resistance games"
            if target.value == "award_best_resistance"
            else "Spy games"
            if target.value == "award_best_spy"
            else "total games"
        )
        await interaction.followup.send(
            (
                f"**{target.name}** now requires at least "
                f"**{games} {unit}**.\n"
                f"Roles updated immediately: **{result.added} added**, "
                f"**{result.removed} removed**."
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="minimum-matches",
        description="Sets the qualifying game minimum for one special award role.",
    )
    @app_commands.describe(
        target="Special role whose qualifying minimum should change.",
        games="Required games. Defaults to ProAvalon's 20-game provisional threshold.",
    )
    @app_commands.choices(target=AWARD_TARGET_CHOICES)
    @bot_admin_only()
    async def minimum_matches(
        self,
        interaction: discord.Interaction,
        target: app_commands.Choice[str],
        games: app_commands.Range[int, 1, 999] = PROVISIONAL_GAMES,
    ) -> None:
        await self._set_minimum_matches_and_sync(
            interaction,
            target,
            int(games),
        )

    @app_commands.command(
        name="minimum-votes",
        description="Sets the eligible crit-vote minimum for a crit accuracy award role.",
    )
    @app_commands.describe(
        target="Crit voting accuracy award role to configure.",
        votes="Minimum eligible crit votes required. Defaults to 15.",
    )
    @app_commands.choices(
        target=[
            app_commands.Choice(
                name="Highest Crit Voting Accuracy",
                value="award_highest_crit_accuracy",
            ),
            app_commands.Choice(
                name="Lowest Crit Voting Accuracy",
                value="award_lowest_crit_accuracy",
            ),
        ],
    )
    @bot_admin_only()
    async def minimum_votes(
        self,
        interaction: discord.Interaction,
        target: app_commands.Choice[str],
        votes: app_commands.Range[int, 1, 999] = 15,
    ) -> None:
        await set_crit_award_minimum_votes(
            interaction.guild_id, target.value, votes
        )
        await interaction.response.send_message(
            f"**{ROLE_TARGET_NAMES[target.value]}** now requires "
            f"at least **{votes} eligible crit votes**.",
            ephemeral=True,
        )
        # Apply removals/additions immediately after a threshold change.
        await self.sync_guild(interaction.guild)

    @app_commands.command(
        name="minimum-games",
        description="Alias for /roles minimum-matches.",
    )
    @app_commands.describe(
        target="Special role whose qualifying minimum should change.",
        games="Required games. Defaults to 20.",
    )
    @app_commands.choices(target=AWARD_TARGET_CHOICES)
    @bot_admin_only()
    async def minimum_games(
        self,
        interaction: discord.Interaction,
        target: app_commands.Choice[str],
        games: app_commands.Range[int, 1, 999] = PROVISIONAL_GAMES,
    ) -> None:
        await self._set_minimum_matches_and_sync(
            interaction,
            target,
            int(games),
        )

    @app_commands.command(
        name="info",
        description="Shows this server's automatic ProAvalon role configuration.",
    )
    @bot_admin_only()
    async def info(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        configs = await get_role_configs(interaction.guild.id)
        award_minimums = await get_all_award_minimum_games(
            interaction.guild.id
        )
        embed = discord.Embed(
            title="Automatic ProAvalon Roles",
            description=(
                "Roles refresh after tracked matches, on `/roles sync`, and "
                "every **15 minutes** as a backup.\n"
                "Default special-role minimum: **20 games**."
            ),
            color=discord.Color.blurple(),
        )
        rank_lines = []
        for key in RANK_KEYS:
            role_id = configs.get(key)
            rank_lines.append(
                f"**{ROLE_TARGET_NAMES[key]}:** "
                + (f"<@&{role_id}>" if role_id else "Not configured")
            )
        award_lines = []
        for key in AWARD_KEYS:
            role_id = configs.get(key)
            line = (
                f"**{ROLE_TARGET_NAMES[key]}:** "
                + (f"<@&{role_id}>" if role_id else "Not configured")
            )
            if key in MINIMUM_AWARD_KEYS:
                minimum = award_minimums[key]
                unit = (
                    "Res GP"
                    if key == "award_best_resistance"
                    else "Spy GP"
                    if key == "award_best_spy"
                    else "GP"
                )
                line += f" • Minimum: **{minimum} {unit}**"
            award_lines.append(line)
        embed.add_field(name="Rank Roles", value="\n".join(rank_lines), inline=False)
        embed.add_field(name="Season Award Roles", value="\n".join(award_lines), inline=False)
        embed.set_footer(
            text="The bot needs Manage Roles and must sit above every managed role."
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="sync",
        description="Immediately refreshes all configured ProAvalon Discord roles.",
    )
    @bot_admin_only()
    async def sync(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        result = await self.sync_guild(interaction.guild)
        award_lines = [
            f"**{ROLE_TARGET_NAMES[key]}:** "
            + ", ".join(f"`{username}`" for username in usernames)
            for key, usernames in result.award_winners.items()
        ]

        embed = discord.Embed(
            title="ProAvalon Role Sync",
            description=(
                f"Checked **{result.members} linked season player(s)**.\n"
                f"Added: **{result.added}** • Removed: **{result.removed}**"
            ),
            color=discord.Color.blurple(),
        )

        def change_field(action: str, symbol: str) -> str | None:
            matching = [
                change for change in result.changes
                if change["action"] == action
            ]
            if not matching:
                return None
            lines = [
                f"{symbol} <@{change['user_id']}> — <@&{change['role_id']}>"
                for change in matching
            ]
            value = "\n".join(lines)
            if len(value) > 1024:
                kept: list[str] = []
                used = 0
                for line in lines:
                    if used + len(line) + 1 > 960:
                        break
                    kept.append(line)
                    used += len(line) + 1
                omitted = len(lines) - len(kept)
                value = "\n".join(kept)
                if omitted:
                    value += f"\n… and {omitted} more."
            return value

        added_value = change_field("added", "➕")
        removed_value = change_field("removed", "➖")
        if added_value:
            embed.add_field(name="Roles Added", value=added_value, inline=False)
        if removed_value:
            embed.add_field(name="Roles Removed", value=removed_value, inline=False)
        if award_lines:
            embed.add_field(
                name="Current Special Role Holders",
                value="\n".join(award_lines)[:1024],
                inline=False,
            )
        if result.profile_failures:
            embed.add_field(
                name="Profile Refresh Warnings",
                value=f"{result.profile_failures} profile(s) could not be refreshed.",
                inline=False,
            )
        if result.errors:
            embed.add_field(
                name="Warnings",
                value="\n".join(f"⚠️ {error}" for error in result.errors[:8])[:1024],
                inline=False,
            )
        if not result.changes:
            embed.set_footer(text="No Discord roles needed to change.")

        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DiscordRoleCommands(bot))
