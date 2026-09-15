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
) -> None:
    database = await connect()
    try:
        await database.execute(
            """
            INSERT INTO recording_integrations (
                guild_id, provider, enabled, account_id, account_name,
                account_url, encrypted_refresh_token, updated_at
            ) VALUES (?, ?, 1, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, provider) DO UPDATE SET
                enabled = 1,
                account_id = excluded.account_id,
                account_name = excluded.account_name,
                account_url = excluded.account_url,
                encrypted_refresh_token = excluded.encrypted_refresh_token,
                updated_at = excluded.updated_at
            """,
            (
                str(guild_id), provider, account_id, account_name,
                account_url, encrypted_refresh_token,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        await database.commit()
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
