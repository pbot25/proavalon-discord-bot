from datetime import datetime, timezone

from database.connection import connect


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def append_recording_audit(
    guild_id: int,
    room_id: int,
    stage: str,
    *,
    recorder_id: str | None = None,
    detail: str | None = None,
) -> None:
    database = await connect()
    try:
        await database.execute(
            """
            INSERT INTO recording_audit_events(
                guild_id, room_id, recorder_id, stage, detail, created_at
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                str(guild_id),
                room_id,
                recorder_id,
                stage,
                detail,
                utc_now(),
            ),
        )
        await database.commit()
    finally:
        await database.close()


async def get_recording_audit(
    guild_id: int,
    room_id: int,
    *,
    limit: int = 20,
):
    database = await connect()
    try:
        cursor = await database.execute(
            """
            SELECT *
            FROM recording_audit_events
            WHERE guild_id = ? AND room_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (str(guild_id), room_id, max(1, min(limit, 50))),
        )
        rows = await cursor.fetchall()
        return list(reversed(rows))
    finally:
        await database.close()
