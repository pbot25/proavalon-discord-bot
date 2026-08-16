from __future__ import annotations

from datetime import datetime, timezone

from database.connection import connect


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _guild_id_text(guild_id: int) -> str:
    if guild_id <= 0:
        raise ValueError("guild_id must be a positive integer.")
    return str(guild_id)


async def get_commands_channel_id(guild_id: int) -> int | None:
    database = await connect()
    try:
        cursor = await database.execute(
            """
            SELECT channel_id
            FROM command_channel_configs
            WHERE guild_id = ?
            LIMIT 1
            """,
            (_guild_id_text(guild_id),),
        )
        row = await cursor.fetchone()
        return None if row is None else int(row["channel_id"])
    finally:
        await database.close()


async def set_commands_channel(guild_id: int, channel_id: int) -> None:
    database = await connect()
    try:
        await database.execute(
            """
            INSERT INTO command_channel_configs (
                guild_id, channel_id, updated_at
            )
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id)
            DO UPDATE SET
                channel_id = excluded.channel_id,
                updated_at = excluded.updated_at
            """,
            (_guild_id_text(guild_id), str(channel_id), utc_now()),
        )
        await database.commit()
    finally:
        await database.close()


async def get_feedback_channel_id(
    guild_id: int,
) -> int | None:
    database = await connect()
    try:
        cursor = await database.execute(
            """
            SELECT channel_id
            FROM feedback_channel_configs
            WHERE guild_id = ?
            LIMIT 1
            """,
            (_guild_id_text(guild_id),),
        )
        row = await cursor.fetchone()
        return None if row is None else int(row["channel_id"])
    finally:
        await database.close()


async def set_feedback_channel(
    guild_id: int,
    channel_id: int,
) -> None:
    database = await connect()
    try:
        await database.execute(
            """
            INSERT INTO feedback_channel_configs (
                guild_id, channel_id, updated_at
            )
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id)
            DO UPDATE SET
                channel_id = excluded.channel_id,
                updated_at = excluded.updated_at
            """,
            (_guild_id_text(guild_id), str(channel_id), utc_now()),
        )
        await database.commit()
    finally:
        await database.close()