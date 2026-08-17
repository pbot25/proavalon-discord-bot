from __future__ import annotations

from datetime import datetime, timezone

from database.connection import connect


RANK_KEYS = (
    "rank_unranked",
    "rank_iron",
    "rank_bronze",
    "rank_silver",
    "rank_gold",
    "rank_platinum",
    "rank_diamond",
    "rank_champion",
)

AWARD_KEYS = (
    "award_best_resistance",
    "award_best_spy",
    "award_highest_rating",
    "award_lowest_rating",
)

ALL_ROLE_KEYS = RANK_KEYS + AWARD_KEYS


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _guild(guild_id: int) -> str:
    return str(guild_id)


async def set_role_config(guild_id: int, role_key: str, role_id: int) -> None:
    if role_key not in ALL_ROLE_KEYS:
        raise ValueError("Unknown Discord role configuration target.")

    database = await connect()
    try:
        cursor = await database.execute(
            """
            SELECT role_key
            FROM discord_role_configs
            WHERE guild_id = ?
              AND role_id = ?
              AND role_key != ?
            LIMIT 1
            """,
            (_guild(guild_id), str(role_id), role_key),
        )
        duplicate = await cursor.fetchone()
        if duplicate is not None:
            raise ValueError(
                "That Discord role is already configured for another "
                "rank/award target. Use a different role."
            )

        await database.execute(
            """
            INSERT INTO discord_role_configs (
                guild_id, role_key, role_id, updated_at
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, role_key)
            DO UPDATE SET
                role_id = excluded.role_id,
                updated_at = excluded.updated_at
            """,
            (_guild(guild_id), role_key, str(role_id), utc_now()),
        )
        await database.commit()
    finally:
        await database.close()


async def clear_role_config(guild_id: int, role_key: str):
    database = await connect()
    try:
        cursor = await database.execute(
            """
            SELECT role_id
            FROM discord_role_configs
            WHERE guild_id = ? AND role_key = ?
            LIMIT 1
            """,
            (_guild(guild_id), role_key),
        )
        existing = await cursor.fetchone()
        if existing is None:
            return None

        await database.execute(
            "DELETE FROM discord_role_configs WHERE guild_id = ? AND role_key = ?",
            (_guild(guild_id), role_key),
        )
        await database.commit()
        return existing
    finally:
        await database.close()


async def get_role_configs(guild_id: int) -> dict[str, int]:
    database = await connect()
    try:
        cursor = await database.execute(
            """
            SELECT role_key, role_id
            FROM discord_role_configs
            WHERE guild_id = ?
            """,
            (_guild(guild_id),),
        )
        rows = await cursor.fetchall()
        return {str(row["role_key"]): int(row["role_id"]) for row in rows}
    finally:
        await database.close()


async def get_award_minimum_games(guild_id: int) -> int:
    database = await connect()
    try:
        cursor = await database.execute(
            """
            SELECT award_minimum_games
            FROM discord_role_settings
            WHERE guild_id = ?
            LIMIT 1
            """,
            (_guild(guild_id),),
        )
        row = await cursor.fetchone()
        return int(row["award_minimum_games"]) if row is not None else 5
    finally:
        await database.close()


async def set_award_minimum_games(guild_id: int, minimum_games: int) -> None:
    database = await connect()
    try:
        await database.execute(
            """
            INSERT INTO discord_role_settings (
                guild_id, award_minimum_games, updated_at
            ) VALUES (?, ?, ?)
            ON CONFLICT(guild_id)
            DO UPDATE SET
                award_minimum_games = excluded.award_minimum_games,
                updated_at = excluded.updated_at
            """,
            (_guild(guild_id), int(minimum_games), utc_now()),
        )
        await database.commit()
    finally:
        await database.close()


async def get_active_season_linked_users(
    guild_id: int,
    season_number: int,
):
    """Return every active Discord ↔ ProAvalon link for this season."""
    database = await connect()
    try:
        cursor = await database.execute(
            """
            SELECT discord_user_id, proavalon_username
            FROM account_links
            WHERE guild_id = ?
              AND season_number = ?
              AND is_active = 1
            ORDER BY proavalon_username COLLATE NOCASE
            """,
            (_guild(guild_id), int(season_number)),
        )
        return await cursor.fetchall()
    finally:
        await database.close()


async def get_known_ratings(usernames: list[str]) -> dict[str, tuple[int, str | None]]:
    if not usernames:
        return {}

    database = await connect()
    try:
        placeholders = ",".join("?" for _ in usernames)
        cursor = await database.execute(
            f"""
            SELECT proavalon_username, rating, rating_bracket
            FROM proavalon_ratings
            WHERE proavalon_username COLLATE NOCASE IN ({placeholders})
            """,
            tuple(usernames),
        )
        rows = await cursor.fetchall()
        return {
            str(row["proavalon_username"]).casefold(): (
                int(row["rating"]),
                row["rating_bracket"],
            )
            for row in rows
        }
    finally:
        await database.close()


async def get_assignment(guild_id: int, assignment_key: str):
    database = await connect()
    try:
        cursor = await database.execute(
            """
            SELECT discord_user_id, role_id
            FROM discord_role_assignments
            WHERE guild_id = ? AND assignment_key = ?
            LIMIT 1
            """,
            (_guild(guild_id), assignment_key),
        )
        return await cursor.fetchone()
    finally:
        await database.close()


async def set_assignment(
    guild_id: int,
    assignment_key: str,
    discord_user_id: int,
    role_id: int,
) -> None:
    database = await connect()
    try:
        await database.execute(
            """
            INSERT INTO discord_role_assignments (
                guild_id, assignment_key, discord_user_id, role_id, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, assignment_key)
            DO UPDATE SET
                discord_user_id = excluded.discord_user_id,
                role_id = excluded.role_id,
                updated_at = excluded.updated_at
            """,
            (
                _guild(guild_id),
                assignment_key,
                str(discord_user_id),
                str(role_id),
                utc_now(),
            ),
        )
        await database.commit()
    finally:
        await database.close()


async def clear_assignment(guild_id: int, assignment_key: str) -> None:
    database = await connect()
    try:
        await database.execute(
            "DELETE FROM discord_role_assignments WHERE guild_id = ? AND assignment_key = ?",
            (_guild(guild_id), assignment_key),
        )
        await database.commit()
    finally:
        await database.close()


async def get_rank_assignments(guild_id: int):
    database = await connect()
    try:
        cursor = await database.execute(
            """
            SELECT assignment_key, discord_user_id, role_id
            FROM discord_role_assignments
            WHERE guild_id = ? AND assignment_key LIKE 'rank:%'
            """,
            (_guild(guild_id),),
        )
        return await cursor.fetchall()
    finally:
        await database.close()
