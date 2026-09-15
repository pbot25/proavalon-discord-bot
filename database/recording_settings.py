from datetime import datetime, timezone

from database.connection import connect


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _guild_id(guild_id: int) -> str:
    if guild_id <= 0:
        raise ValueError("guild_id must be a positive integer")
    return str(guild_id)


async def get_recording_config(guild_id: int):
    database = await connect()
    try:
        cursor = await database.execute(
            "SELECT * FROM recording_configs WHERE guild_id = ? LIMIT 1",
            (_guild_id(guild_id),),
        )
        return await cursor.fetchone()
    finally:
        await database.close()


async def set_recording_enabled(guild_id: int, enabled: bool) -> None:
    database = await connect()
    try:
        await database.execute(
            """
            INSERT INTO recording_configs (guild_id, enabled, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                enabled = excluded.enabled,
                updated_at = excluded.updated_at
            """,
            (_guild_id(guild_id), int(enabled), _utc_now()),
        )
        await database.commit()
    finally:
        await database.close()


async def set_recording_output_channel(
    guild_id: int,
    channel_id: int | None,
) -> None:
    database = await connect()
    try:
        await database.execute(
            """
            INSERT INTO recording_configs (
                guild_id,
                enabled,
                output_channel_id,
                updated_at
            )
            VALUES (?, 0, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                output_channel_id = excluded.output_channel_id,
                updated_at = excluded.updated_at
            """,
            (
                _guild_id(guild_id),
                str(channel_id) if channel_id is not None else None,
                _utc_now(),
            ),
        )
        await database.commit()
    finally:
        await database.close()
