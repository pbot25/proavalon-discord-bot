from database.connection import connect
from database.seasons import utc_now


def _guild_id_text(guild_id: int) -> str:
    if guild_id <= 0:
        raise ValueError("guild_id must be a positive integer.")
    return str(guild_id)


async def is_bot_admin(
    guild_id: int,
    discord_user_id: int,
) -> bool:
    database = await connect()

    try:
        cursor = await database.execute(
            """
            SELECT 1
            FROM bot_admins
            WHERE guild_id = ?
              AND discord_user_id = ?
            LIMIT 1
            """,
            (
                _guild_id_text(guild_id),
                str(discord_user_id),
            ),
        )

        return await cursor.fetchone() is not None
    finally:
        await database.close()


async def add_bot_admin(
    guild_id: int,
    discord_user_id: int,
    added_by_discord_user_id: int,
) -> None:
    database = await connect()

    try:
        await database.execute(
            """
            INSERT INTO bot_admins (
                guild_id,
                discord_user_id,
                added_at,
                added_by_discord_user_id
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, discord_user_id) DO NOTHING
            """,
            (
                _guild_id_text(guild_id),
                str(discord_user_id),
                utc_now(),
                str(added_by_discord_user_id),
            ),
        )

        await database.commit()
    finally:
        await database.close()


async def remove_bot_admin(
    guild_id: int,
    discord_user_id: int,
) -> bool:
    database = await connect()

    try:
        cursor = await database.execute(
            """
            DELETE FROM bot_admins
            WHERE guild_id = ?
              AND discord_user_id = ?
            """,
            (
                _guild_id_text(guild_id),
                str(discord_user_id),
            ),
        )

        await database.commit()
        return cursor.rowcount > 0
    finally:
        await database.close()


async def get_all_bot_admin_ids(
    guild_id: int,
) -> list[int]:
    database = await connect()

    try:
        cursor = await database.execute(
            """
            SELECT discord_user_id
            FROM bot_admins
            WHERE guild_id = ?
            ORDER BY added_at ASC
            """,
            (_guild_id_text(guild_id),),
        )

        rows = await cursor.fetchall()

        return [
            int(row["discord_user_id"])
            for row in rows
        ]
    finally:
        await database.close()
