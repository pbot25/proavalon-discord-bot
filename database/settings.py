from datetime import datetime, timezone

from database.connection import connect


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _guild_id_text(guild_id: int) -> str:
    if guild_id <= 0:
        raise ValueError("guild_id must be a positive integer.")

    return str(guild_id)


async def get_guild_timezone(
    guild_id: int,
) -> str | None:
    database = await connect()

    try:
        cursor = await database.execute(
            """
            SELECT timezone
            FROM guild_settings
            WHERE guild_id = ?
            LIMIT 1
            """,
            (_guild_id_text(guild_id),),
        )

        row = await cursor.fetchone()

        if row is None:
            return None

        return str(row["timezone"])

    finally:
        await database.close()


async def set_guild_timezone(
    guild_id: int,
    timezone_name: str,
) -> None:
    database = await connect()

    try:
        await database.execute(
            """
            INSERT INTO guild_settings (
                guild_id,
                timezone,
                updated_at
            )
            VALUES (?, ?, ?)

            ON CONFLICT(guild_id)
            DO UPDATE SET
                timezone = excluded.timezone,
                updated_at = excluded.updated_at
            """,
            (
                _guild_id_text(guild_id),
                timezone_name,
                utc_now(),
            ),
        )

        await database.commit()

    finally:
        await database.close()


async def _ensure_daily_leaderboard_channels_schema(
    database,
) -> None:
    """
    Upgrade an existing daily_leaderboard_channels table from the original
    four-statistic CHECK constraint to the complete /leaderboard statistic
    set without deleting any configured daily leaderboards.
    """
    cursor = await database.execute(
        """
        SELECT sql
        FROM sqlite_master
        WHERE type = 'table'
          AND name = 'daily_leaderboard_channels'
        LIMIT 1
        """
    )
    row = await cursor.fetchone()

    if row is None:
        return

    table_sql = str(row["sql"] or "")

    # Presence of these values means the expanded schema is already active.
    if "games_played" in table_sql and "oberon" in table_sql:
        return

    await database.execute(
        "ALTER TABLE daily_leaderboard_channels "
        "RENAME TO daily_leaderboard_channels_old"
    )

    await database.execute(
        """
        CREATE TABLE daily_leaderboard_channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id TEXT NOT NULL,
            leaderboard_type TEXT NOT NULL
                CHECK (
                    leaderboard_type IN (
                        'total_time',
                        'games_played',
                        'rating',
                        'overall',
                        'resistance',
                        'spy',
                        'vt',
                        'lover',
                        'percival',
                        'merlin',
                        'assassin',
                        'morgana',
                        'mordred',
                        'oberon'
                    )
                ),
            channel_id TEXT NOT NULL,
            post_time TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1
                CHECK (enabled IN (0, 1)),
            minimum_games INTEGER NOT NULL DEFAULT 5,
            top_count INTEGER NOT NULL DEFAULT 500,
            last_post_local_date TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )

    await database.execute(
        """
        INSERT INTO daily_leaderboard_channels (
            id,
            guild_id,
            leaderboard_type,
            channel_id,
            post_time,
            enabled,
            minimum_games,
            top_count,
            last_post_local_date,
            updated_at
        )
        SELECT
            id,
            guild_id,
            leaderboard_type,
            channel_id,
            post_time,
            enabled,
            minimum_games,
            top_count,
            last_post_local_date,
            updated_at
        FROM daily_leaderboard_channels_old
        """
    )

    await database.execute(
        "DROP TABLE daily_leaderboard_channels_old"
    )

    await database.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS
            one_daily_leaderboard_per_guild_channel
        ON daily_leaderboard_channels (
            guild_id,
            channel_id
        )
        """
    )

    await database.execute(
        """
        CREATE INDEX IF NOT EXISTS
            daily_leaderboard_channels_enabled
        ON daily_leaderboard_channels (
            enabled,
            guild_id
        )
        """
    )

    await database.commit()


async def _migrate_legacy_daily_leaderboard(
    database,
    guild_id: int | None = None,
) -> None:
    await _ensure_daily_leaderboard_channels_schema(database)

    if guild_id is None:
        where_clause = ""
        params = ()
    else:
        where_clause = "WHERE guild_id = ?"
        params = (_guild_id_text(guild_id),)

    cursor = await database.execute(
        f"""
        SELECT
            guild_id,
            leaderboard_type,
            channel_id,
            post_time,
            enabled,
            minimum_games,
            top_count,
            last_post_local_date,
            updated_at
        FROM daily_leaderboard_configs
        {where_clause}
        """,
        params,
    )
    rows = await cursor.fetchall()

    for row in rows:
        await database.execute(
            """
            INSERT OR IGNORE INTO daily_leaderboard_channels (
                guild_id,
                leaderboard_type,
                channel_id,
                post_time,
                enabled,
                minimum_games,
                top_count,
                last_post_local_date,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["guild_id"],
                row["leaderboard_type"],
                row["channel_id"],
                row["post_time"],
                row["enabled"],
                row["minimum_games"],
                row["top_count"],
                row["last_post_local_date"],
                row["updated_at"],
            ),
        )


async def get_daily_leaderboard_configs(
    guild_id: int,
):
    database = await connect()
    try:
        await _migrate_legacy_daily_leaderboard(database, guild_id)
        await database.commit()

        cursor = await database.execute(
            """
            SELECT
                id,
                guild_id,
                leaderboard_type,
                channel_id,
                post_time,
                enabled,
                minimum_games,
                top_count,
                last_post_local_date,
                updated_at
            FROM daily_leaderboard_channels
            WHERE guild_id = ?
            ORDER BY enabled DESC, id ASC
            """,
            (_guild_id_text(guild_id),),
        )
        return await cursor.fetchall()
    finally:
        await database.close()


async def set_daily_leaderboard_config(
    guild_id: int,
    leaderboard_type: str,
    channel_id: int,
    post_time: str,
    *,
    minimum_games: int = 5,
    top_count: int = 500,
) -> int:
    database = await connect()
    try:
        await _migrate_legacy_daily_leaderboard(database, guild_id)

        await database.execute(
            """
            INSERT INTO daily_leaderboard_channels (
                guild_id,
                leaderboard_type,
                channel_id,
                post_time,
                enabled,
                minimum_games,
                top_count,
                last_post_local_date,
                updated_at
            )
            VALUES (?, ?, ?, ?, 1, ?, ?, NULL, ?)

            ON CONFLICT(guild_id, channel_id)
            DO UPDATE SET
                leaderboard_type = excluded.leaderboard_type,
                post_time = excluded.post_time,
                enabled = 1,
                minimum_games = excluded.minimum_games,
                top_count = excluded.top_count,
                last_post_local_date = NULL,
                updated_at = excluded.updated_at
            """,
            (
                _guild_id_text(guild_id),
                leaderboard_type,
                str(channel_id),
                post_time,
                minimum_games,
                top_count,
                utc_now(),
            ),
        )
        await database.commit()

        cursor = await database.execute(
            """
            SELECT id
            FROM daily_leaderboard_channels
            WHERE guild_id = ?
              AND channel_id = ?
            LIMIT 1
            """,
            (_guild_id_text(guild_id), str(channel_id)),
        )
        row = await cursor.fetchone()
        return int(row["id"])
    finally:
        await database.close()


async def disable_daily_leaderboard(
    guild_id: int,
    channel_id: int | None = None,
) -> int:
    database = await connect()
    try:
        await _migrate_legacy_daily_leaderboard(database, guild_id)

        if channel_id is None:
            cursor = await database.execute(
                """
                UPDATE daily_leaderboard_channels
                SET enabled = 0,
                    updated_at = ?
                WHERE guild_id = ?
                  AND enabled = 1
                """,
                (utc_now(), _guild_id_text(guild_id)),
            )
        else:
            cursor = await database.execute(
                """
                UPDATE daily_leaderboard_channels
                SET enabled = 0,
                    updated_at = ?
                WHERE guild_id = ?
                  AND channel_id = ?
                  AND enabled = 1
                """,
                (
                    utc_now(),
                    _guild_id_text(guild_id),
                    str(channel_id),
                ),
            )

        await database.commit()
        return int(cursor.rowcount)
    finally:
        await database.close()


async def get_enabled_daily_leaderboards():
    database = await connect()
    try:
        await _migrate_legacy_daily_leaderboard(database)
        await database.commit()

        cursor = await database.execute(
            """
            SELECT
                daily_leaderboard_channels.id,
                daily_leaderboard_channels.guild_id,
                daily_leaderboard_channels.leaderboard_type,
                daily_leaderboard_channels.channel_id,
                daily_leaderboard_channels.post_time,
                daily_leaderboard_channels.enabled,
                daily_leaderboard_channels.minimum_games,
                daily_leaderboard_channels.top_count,
                daily_leaderboard_channels.last_post_local_date,
                daily_leaderboard_channels.updated_at,
                guild_settings.timezone
            FROM daily_leaderboard_channels
            INNER JOIN guild_settings
                ON guild_settings.guild_id =
                    daily_leaderboard_channels.guild_id
            WHERE daily_leaderboard_channels.enabled = 1
            ORDER BY daily_leaderboard_channels.guild_id,
                     daily_leaderboard_channels.id
            """
        )
        return await cursor.fetchall()
    finally:
        await database.close()


async def mark_daily_leaderboard_posted(
    config_id: int,
    local_date: str,
) -> None:
    database = await connect()
    try:
        await database.execute(
            """
            UPDATE daily_leaderboard_channels
            SET last_post_local_date = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (local_date, utc_now(), config_id),
        )
        await database.commit()
    finally:
        await database.close()