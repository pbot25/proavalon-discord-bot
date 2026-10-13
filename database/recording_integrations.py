from __future__ import annotations

from datetime import datetime, timezone

from database.connection import connect


async def get_recording_integration(guild_id: int, provider: str):
    database = await connect()
    try:
        cursor = await database.execute(
            "SELECT * FROM recording_integrations WHERE guild_id = ? AND provider = ?",
            (str(guild_id), provider),
        )
        return await cursor.fetchone()
    finally:
        await database.close()


async def save_recording_integration(
    guild_id: int,
    provider: str,
    account_id: str,
    account_name: str,
    account_url: str,
    encrypted_refresh_token: str,
    authorized_user_id: int | None = None,
) -> None:
    database = await connect()
    try:
        await database.execute(
            """
            INSERT INTO recording_integrations (
                guild_id, provider, enabled, account_id, account_name,
                account_url, encrypted_refresh_token, authorized_user_id,
                expiration_reminder_sent_at, updated_at
            ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, NULL, ?)
            ON CONFLICT(guild_id, provider) DO UPDATE SET
                enabled = 1,
                account_id = excluded.account_id,
                account_name = excluded.account_name,
                account_url = excluded.account_url,
                encrypted_refresh_token = excluded.encrypted_refresh_token,
                authorized_user_id = excluded.authorized_user_id,
                expiration_reminder_sent_at = NULL,
                updated_at = excluded.updated_at
            """,
            (
                str(guild_id), provider, account_id, account_name,
                account_url, encrypted_refresh_token,
                str(authorized_user_id) if authorized_user_id is not None else None,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        await database.commit()
    finally:
        await database.close()


async def get_pending_youtube_expiration_reminders():
    database = await connect()
    try:
        cursor = await database.execute(
            """
            SELECT integration.*, config.output_channel_id
            FROM recording_integrations AS integration
            LEFT JOIN recording_configs AS config
              ON config.guild_id = integration.guild_id
            WHERE integration.provider = 'youtube'
              AND integration.enabled = 1
              AND integration.authorized_user_id IS NOT NULL
              AND integration.expiration_reminder_sent_at IS NULL
            """
        )
        return await cursor.fetchall()
    finally:
        await database.close()


async def mark_youtube_expiration_reminder_sent(
    guild_id: int,
    connected_at: str,
) -> bool:
    """Mark only the same connection, avoiding races with a fresh reconnect."""
    database = await connect()
    try:
        cursor = await database.execute(
            """
            UPDATE recording_integrations
            SET expiration_reminder_sent_at = ?
            WHERE guild_id = ?
              AND provider = 'youtube'
              AND updated_at = ?
              AND expiration_reminder_sent_at IS NULL
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                str(guild_id),
                connected_at,
            ),
        )
        await database.commit()
        return cursor.rowcount > 0
    finally:
        await database.close()


async def delete_recording_integration(guild_id: int, provider: str) -> None:
    database = await connect()
    try:
        await database.execute(
            "DELETE FROM recording_integrations WHERE guild_id = ? AND provider = ?",
            (str(guild_id), provider),
        )
        await database.commit()
    finally:
        await database.close()
