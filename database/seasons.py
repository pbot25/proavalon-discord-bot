from datetime import datetime, timezone

from database.connection import connect


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _guild_id_text(guild_id: int) -> str:
    if guild_id <= 0:
        raise ValueError("guild_id must be a positive integer.")
    return str(guild_id)


async def get_active_season(
    guild_id: int,
):
    database = await connect()

    try:
        cursor = await database.execute(
            """
            SELECT
                id,
                guild_id,
                season_number,
                required_prefix,
                tracked_player_count,
                started_at,
                ended_at,
                is_active
            FROM community_seasons
            WHERE guild_id = ?
              AND is_active = 1
            LIMIT 1
            """,
            (_guild_id_text(guild_id),),
        )

        return await cursor.fetchone()
    finally:
        await database.close()


async def get_season_by_number(
    guild_id: int,
    season_number: int,
):
    database = await connect()

    try:
        cursor = await database.execute(
            """
            SELECT
                id,
                guild_id,
                season_number,
                required_prefix,
                tracked_player_count,
                started_at,
                ended_at,
                is_active
            FROM community_seasons
            WHERE guild_id = ?
              AND season_number = ?
            LIMIT 1
            """,
            (
                _guild_id_text(guild_id),
                season_number,
            ),
        )

        return await cursor.fetchone()
    finally:
        await database.close()


async def get_season_by_prefix(
    guild_id: int,
    required_prefix: str,
):
    database = await connect()
    try:
        cursor = await database.execute(
            """
            SELECT
                id, guild_id, season_number, required_prefix,
                tracked_player_count, started_at, ended_at, is_active
            FROM community_seasons
            WHERE guild_id = ?
              AND required_prefix = ? COLLATE NOCASE
            LIMIT 1
            """,
            (_guild_id_text(guild_id), required_prefix.strip()),
        )
        return await cursor.fetchone()
    finally:
        await database.close()


async def get_latest_season(
    guild_id: int,
):
    database = await connect()

    try:
        cursor = await database.execute(
            """
            SELECT
                id,
                guild_id,
                season_number,
                required_prefix,
                tracked_player_count,
                started_at,
                ended_at,
                is_active
            FROM community_seasons
            WHERE guild_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (_guild_id_text(guild_id),),
        )

        return await cursor.fetchone()
    finally:
        await database.close()


async def create_season(
    guild_id: int,
    required_prefix: str,
):
    """
    Create or resume a community season for one Discord guild.

    Prefixes are case-insensitive public identifiers. The existing
    season_number column remains a stable internal numeric scope key so
    archived account links and existing season data stay compatible.
    """
    canonical_prefix = required_prefix.strip()

    if (
        not canonical_prefix
        or len(canonical_prefix) > 20
        or not canonical_prefix.isalnum()
    ):
        raise ValueError(
            "Season prefix must be 1-20 alphanumeric characters."
        )

    guild_text = _guild_id_text(guild_id)
    active_season = await get_active_season(guild_id)

    if active_season is not None:
        raise RuntimeError("End the current season first.")

    database = await connect()

    try:
        existing_cursor = await database.execute(
            """
            SELECT
                id, guild_id, season_number, required_prefix,
                tracked_player_count, started_at, ended_at, is_active
            FROM community_seasons
            WHERE guild_id = ?
              AND required_prefix = ? COLLATE NOCASE
            LIMIT 1
            """,
            (guild_text, canonical_prefix),
        )
        existing_season = await existing_cursor.fetchone()

        latest_cursor = await database.execute(
            """
            SELECT id, season_number, required_prefix
            FROM community_seasons
            WHERE guild_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (guild_text,),
        )
        latest_season = await latest_cursor.fetchone()

        if existing_season is not None:
            if (
                latest_season is not None
                and latest_season["id"] != existing_season["id"]
            ):
                raise RuntimeError(
                    (
                        f"Season `{existing_season['required_prefix']}` is "
                        "archived and cannot be resumed because a newer "
                        "season has already begun in this server."
                    )
                )

            await database.execute(
                """
                UPDATE community_seasons
                SET is_active = 1,
                    ended_at = NULL
                WHERE id = ?
                  AND guild_id = ?
                """,
                (existing_season["id"], guild_text),
            )
            await database.commit()

            reopened_cursor = await database.execute(
                """
                SELECT
                    id, guild_id, season_number, required_prefix,
                    tracked_player_count, started_at, ended_at, is_active
                FROM community_seasons
                WHERE id = ?
                  AND guild_id = ?
                """,
                (existing_season["id"], guild_text),
            )
            return await reopened_cursor.fetchone()

        next_cursor = await database.execute(
            """
            SELECT COALESCE(MAX(season_number), 0) + 1 AS next_number
            FROM community_seasons
            WHERE guild_id = ?
            """,
            (guild_text,),
        )
        next_row = await next_cursor.fetchone()
        internal_number = int(next_row["next_number"])

        cursor = await database.execute(
            """
            INSERT INTO community_seasons (
                guild_id,
                season_number,
                required_prefix,
                tracked_player_count,
                started_at,
                ended_at,
                is_active
            )
            VALUES (?, ?, ?, 10, ?, NULL, 1)
            """,
            (
                guild_text,
                internal_number,
                canonical_prefix,
                utc_now(),
            ),
        )
        await database.commit()

        created_cursor = await database.execute(
            """
            SELECT
                id, guild_id, season_number, required_prefix,
                tracked_player_count, started_at, ended_at, is_active
            FROM community_seasons
            WHERE id = ?
              AND guild_id = ?
            """,
            (cursor.lastrowid, guild_text),
        )
        return await created_cursor.fetchone()
    finally:
        await database.close()


async def end_active_season(
    guild_id: int,
):
    active_season = await get_active_season(guild_id)

    if active_season is None:
        return None

    database = await connect()

    try:
        now = utc_now()
        guild_text = _guild_id_text(guild_id)

        await database.execute(
            """
            UPDATE community_seasons
            SET
                is_active = 0,
                ended_at = ?
            WHERE id = ?
              AND guild_id = ?
            """,
            (
                now,
                active_season["id"],
                guild_text,
            ),
        )

        # Season-scoped automatic posting must never outlive the season.
        await database.execute(
            """
            UPDATE daily_leaderboard_configs
            SET enabled = 0, updated_at = ?
            WHERE guild_id = ?
            """,
            (now, guild_text),
        )
        await database.execute(
            """
            UPDATE season_match_configs
            SET enabled = 0, updated_at = ?
            WHERE guild_id = ?
            """,
            (now, guild_text),
        )

        await database.commit()
        return active_season
    finally:
        await database.close()


async def is_season_player_voided(
    community_season_id: int,
    proavalon_username: str,
) -> bool:
    database = await connect()

    try:
        cursor = await database.execute(
            """
            SELECT 1
            FROM season_voided_players
            WHERE community_season_id = ?
              AND proavalon_username = ? COLLATE NOCASE
            LIMIT 1
            """,
            (
                community_season_id,
                proavalon_username.strip(),
            ),
        )

        return await cursor.fetchone() is not None

    finally:
        await database.close()


async def void_season_player(
    community_season_id: int,
    proavalon_username: str,
    voided_by_discord_user_id: int,
) -> bool:
    """
    Exclude a username from one community season.

    The username is also removed from season_players immediately so it
    disappears from registered-player lists, comparisons, leaderboards,
    and daily leaderboard snapshots.

    Returns True if a new void record was created, or False if the
    username was already voided.
    """

    canonical_username = proavalon_username.strip()

    if not canonical_username:
        raise ValueError(
            "ProAvalon username cannot be empty."
        )

    database = await connect()

    try:
        await database.execute("BEGIN")

        existing_cursor = await database.execute(
            """
            SELECT 1
            FROM season_voided_players
            WHERE community_season_id = ?
              AND proavalon_username = ? COLLATE NOCASE
            LIMIT 1
            """,
            (
                community_season_id,
                canonical_username,
            ),
        )

        already_voided = (
            await existing_cursor.fetchone()
            is not None
        )

        if not already_voided:
            await database.execute(
                """
                INSERT INTO season_voided_players (
                    community_season_id,
                    proavalon_username,
                    voided_by_discord_user_id,
                    voided_at
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    community_season_id,
                    canonical_username,
                    str(voided_by_discord_user_id),
                    utc_now(),
                ),
            )

        # Remove it from the live season roster even if it had already
        # registered previously.
        await database.execute(
            """
            DELETE FROM season_players
            WHERE community_season_id = ?
              AND proavalon_username = ? COLLATE NOCASE
            """,
            (
                community_season_id,
                canonical_username,
            ),
        )

        await database.commit()

        return not already_voided

    except Exception:
        await database.rollback()
        raise

    finally:
        await database.close()


async def unvoid_season_player(
    community_season_id: int,
    proavalon_username: str,
):
    """
    Remove a username from the season void list.

    This does not automatically re-register the player. The account may
    register normally again the next time it is looked up with /stats
    player and satisfies the season requirements.
    """

    database = await connect()

    try:
        cursor = await database.execute(
            """
            SELECT
                id,
                community_season_id,
                proavalon_username,
                voided_by_discord_user_id,
                voided_at
            FROM season_voided_players
            WHERE community_season_id = ?
              AND proavalon_username = ? COLLATE NOCASE
            LIMIT 1
            """,
            (
                community_season_id,
                proavalon_username.strip(),
            ),
        )

        row = await cursor.fetchone()

        if row is None:
            return None

        await database.execute(
            """
            DELETE FROM season_voided_players
            WHERE id = ?
            """,
            (row["id"],),
        )

        await database.commit()
        return row

    finally:
        await database.close()


async def get_voided_season_players(
    community_season_id: int,
):
    database = await connect()

    try:
        cursor = await database.execute(
            """
            SELECT
                id,
                community_season_id,
                proavalon_username,
                voided_by_discord_user_id,
                voided_at
            FROM season_voided_players
            WHERE community_season_id = ?
            ORDER BY proavalon_username COLLATE NOCASE ASC
            """,
            (community_season_id,),
        )

        return await cursor.fetchall()

    finally:
        await database.close()


async def count_voided_season_players(
    community_season_id: int,
) -> int:
    database = await connect()

    try:
        cursor = await database.execute(
            """
            SELECT COUNT(*) AS total
            FROM season_voided_players
            WHERE community_season_id = ?
            """,
            (community_season_id,),
        )

        row = await cursor.fetchone()
        return int(row["total"])

    finally:
        await database.close()


async def register_season_player(
    community_season_id: int,
    proavalon_username: str,
) -> bool:
    """
    Register a player unless that username has been explicitly voided
    from this season.

    Returns True when the username is allowed in the season and False
    when it is currently voided.
    """

    canonical_username = proavalon_username.strip()

    if not canonical_username:
        raise ValueError(
            "ProAvalon username cannot be empty."
        )

    if await is_season_player_voided(
        community_season_id,
        canonical_username,
    ):
        return False

    timestamp = utc_now()
    database = await connect()

    try:
        await database.execute(
            """
            INSERT INTO season_players (
                community_season_id,
                proavalon_username,
                first_seen_at,
                last_seen_at
            )
            VALUES (?, ?, ?, ?)

            ON CONFLICT (
                community_season_id,
                proavalon_username
            )
            DO UPDATE SET
                proavalon_username =
                    excluded.proavalon_username,
                last_seen_at =
                    excluded.last_seen_at
            """,
            (
                community_season_id,
                canonical_username,
                timestamp,
                timestamp,
            ),
        )

        await database.commit()
        return True

    finally:
        await database.close()


async def count_season_players(
    community_season_id: int,
) -> int:
    database = await connect()

    try:
        cursor = await database.execute(
            """
            SELECT COUNT(*) AS total
            FROM season_players
            WHERE community_season_id = ?
            """,
            (community_season_id,),
        )

        row = await cursor.fetchone()
        return int(row["total"])
    finally:
        await database.close()


async def get_season_players(
    community_season_id: int,
):
    database = await connect()

    try:
        cursor = await database.execute(
            """
            SELECT
                id,
                community_season_id,
                proavalon_username,
                first_seen_at,
                last_seen_at
            FROM season_players
            WHERE community_season_id = ?
            ORDER BY proavalon_username COLLATE NOCASE ASC
            """,
            (community_season_id,),
        )

        return await cursor.fetchall()
    finally:
        await database.close()


async def reset_all_season_data(
    guild_id: int,
) -> None:
    """
    Permanently delete all season history for one Discord guild only.

    Other guilds, their seasons, account links, and bot administrators
    are not affected.
    """

    guild_text = _guild_id_text(guild_id)
    database = await connect()

    try:
        await database.execute("BEGIN")

        season_cursor = await database.execute(
            """
            SELECT id
            FROM community_seasons
            WHERE guild_id = ?
            """,
            (guild_text,),
        )
        season_rows = await season_cursor.fetchall()
        season_ids = [row["id"] for row in season_rows]

        for season_id in season_ids:
            await database.execute(
                """
                DELETE FROM season_players
                WHERE community_season_id = ?
                """,
                (season_id,),
            )

        # Disable season-scoped scheduled output before deleting season history.
        now = utc_now()
        await database.execute(
            """
            UPDATE daily_leaderboard_configs
            SET enabled = 0, updated_at = ?
            WHERE guild_id = ?
            """,
            (now, guild_text),
        )
        await database.execute(
            """
            UPDATE season_match_configs
            SET enabled = 0, updated_at = ?
            WHERE guild_id = ?
            """,
            (now, guild_text),
        )

        await database.execute(
            """
            DELETE FROM community_seasons
            WHERE guild_id = ?
            """,
            (guild_text,),
        )

        await database.commit()

    except Exception:
        await database.rollback()
        raise

    finally:
        await database.close()


async def get_all_active_seasons():
    database = await connect()
    try:
        cursor = await database.execute("""SELECT id,guild_id,season_number,required_prefix,tracked_player_count,started_at,ended_at,is_active FROM community_seasons WHERE is_active=1""")
        return await cursor.fetchall()
    finally:
        await database.close()