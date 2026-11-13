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


async def get_room_recording_start_times(
    guild_ids: list[int],
    room_id: int,
    *,
    since: str,
) -> list[str]:
    """Return persisted start observations for one recent room lifecycle."""
    guild_texts = [str(guild_id) for guild_id in guild_ids]
    if not guild_texts:
        return []
    placeholders = ",".join("?" for _ in guild_texts)
    database = await connect()
    try:
        cursor = await database.execute(
            f"""
            SELECT stage, detail, created_at
            FROM recording_audit_events
            WHERE guild_id IN ({placeholders})
              AND room_id = ?
              AND created_at >= ?
              AND stage IN ('match_tracking_started', 'game_recording_started')
            ORDER BY created_at ASC, id ASC
            """,
            (*guild_texts, room_id, since),
        )
        rows = await cursor.fetchall()
        starts: list[str] = []
        for row in rows:
            # Pbot persists its original socket observation in detail. Craig's
            # game_recording_started event already has the correct created_at.
            value = (
                row["detail"]
                if row["stage"] == "match_tracking_started" and row["detail"]
                else row["created_at"]
            )
            starts.append(str(value))
        return starts
    finally:
        await database.close()
