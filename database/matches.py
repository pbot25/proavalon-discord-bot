from datetime import datetime, timezone
import json
from database.connection import connect

def utc_now(): return datetime.now(timezone.utc).isoformat()

async def set_match_channel(guild_id:int, channel_id:int):
    db=await connect()
    try:
        await db.execute("""INSERT INTO season_match_configs(guild_id,channel_id,enabled,updated_at) VALUES(?,?,1,?) ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id, enabled=1, updated_at=excluded.updated_at""",(str(guild_id),str(channel_id),utc_now()))
        await db.commit()
    finally: await db.close()

async def get_match_config(guild_id:int):
    db=await connect()
    try:
        c=await db.execute("SELECT * FROM season_match_configs WHERE guild_id=?",(str(guild_id),)); return await c.fetchone()
    finally: await db.close()

async def get_enabled_match_configs():
    db=await connect()
    try:
        c=await db.execute("SELECT * FROM season_match_configs WHERE enabled=1"); return await c.fetchall()
    finally: await db.close()

async def disable_match_channel(guild_id:int):
    db=await connect()
    try:
        c=await db.execute("UPDATE season_match_configs SET enabled=0, updated_at=? WHERE guild_id=? AND enabled=1",(utc_now(),str(guild_id))); await db.commit(); return c.rowcount>0
    finally: await db.close()

async def save_match(
    *,
    season_id: int,
    fingerprint: str,
    room_id: int,
    detected_at: str,
    winner,
    game_mode,
    mission_history,
    fails_history,
    vote_history,
    players,
    roles,
    rating_changes,
    game_duration_seconds: float | None = None,
    assassination_duration_seconds: float | None = None,
):
    """Archive a match and return its season-local public match ID, or None if duplicate."""
    db=await connect()
    try:
        await db.execute("BEGIN IMMEDIATE")

        existing = await db.execute(
            """
            SELECT season_match_id
            FROM season_matches
            WHERE community_season_id = ?
              AND match_fingerprint = ?
            LIMIT 1
            """,
            (season_id, fingerprint),
        )
        existing_row = await existing.fetchone()
        if existing_row is not None:
            await db.rollback()
            return None

        cursor = await db.execute(
            """
            SELECT COALESCE(MAX(season_match_id), 0) + 1 AS next_match_id
            FROM season_matches
            WHERE community_season_id = ?
            """,
            (season_id,),
        )
        next_row = await cursor.fetchone()
        match_id = int(next_row["next_match_id"])

        await db.execute(
            """
            INSERT INTO season_matches(
                community_season_id, season_match_id, match_fingerprint,
                room_id, detected_at, finished_at, winner, game_mode,
                mission_history_json, fails_history_json, vote_history_json,
                players_json, roles_json, rating_changes_json,
                game_duration_seconds, assassination_duration_seconds
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                season_id, match_id, fingerprint, room_id, detected_at,
                utc_now(), winner, game_mode, json.dumps(mission_history),
                json.dumps(fails_history),
                json.dumps(vote_history) if vote_history is not None else None,
                json.dumps(players),
                json.dumps(roles) if roles is not None else None,
                json.dumps(rating_changes),
                game_duration_seconds,
                assassination_duration_seconds,
            ),
        )
        await db.commit()
        return match_id
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()


async def get_previous_player_brackets(
    season_id: int,
    current_fingerprint: str,
    usernames: list[str],
) -> dict[str, str]:
    """
    Return each player's most recently archived rating bracket before the
    current match.

    Match-history announcements use this instead of trusting only the live
    pre-game snapshot. That prevents an old promotion/demotion from being
    announced again if ProAvalon's live rating feed was briefly stale.
    """
    targets = {
        username.strip().casefold()
        for username in usernames
        if isinstance(username, str) and username.strip()
    }
    if not targets:
        return {}

    db = await connect()
    try:
        cursor = await db.execute(
            """
            SELECT rating_changes_json
            FROM season_matches
            WHERE community_season_id = ?
              AND match_fingerprint != ?
            ORDER BY finished_at DESC, id DESC
            """,
            (season_id, current_fingerprint),
        )
        rows = await cursor.fetchall()
    finally:
        await db.close()

    found: dict[str, str] = {}
    for row in rows:
        raw = row["rating_changes_json"]
        if not raw:
            continue
        try:
            changes = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(changes, list):
            continue

        for item in changes:
            if not isinstance(item, dict):
                continue
            username = item.get("username")
            bracket = item.get("new_bracket")
            if not isinstance(username, str) or not username.strip():
                continue
            key = username.strip().casefold()
            if key not in targets or key in found:
                continue
            if isinstance(bracket, str) and bracket.strip():
                found[key] = bracket.strip().casefold()

        if targets.issubset(found):
            break

    return found

async def mark_match_posted(season_id:int, fingerprint:str, channel_id:int|None=None, message_id:int|None=None):
    db=await connect()
    try:
        await db.execute("UPDATE season_matches SET posted_at=?,posted_channel_id=?,posted_message_id=? WHERE community_season_id=? AND match_fingerprint=?",(utc_now(),str(channel_id) if channel_id is not None else None,str(message_id) if message_id is not None else None,season_id,fingerprint)); await db.commit()
    finally: await db.close()

async def get_match_for_recording(guild_id:int,room_id:int):
    db=await connect()
    try:
        c=await db.execute("SELECT sm.* FROM season_matches sm JOIN community_seasons cs ON cs.id=sm.community_season_id WHERE cs.guild_id=? AND sm.room_id=? ORDER BY sm.finished_at DESC,sm.id DESC LIMIT 1",(str(guild_id),room_id)); return await c.fetchone()
    finally: await db.close()

async def set_recording_status(match_database_id:int,status:str):
    db=await connect()
    try:
        await db.execute("UPDATE season_matches SET recording_status=? WHERE id=?",(status,match_database_id)); await db.commit()
    finally: await db.close()

async def set_recording_status_by_season_match(season_id:int,match_id:int,status:str):
    db=await connect()
    try:
        await db.execute("UPDATE season_matches SET recording_status=? WHERE community_season_id=? AND season_match_id=?",(status,season_id,match_id)); await db.commit()
    finally: await db.close()

async def save_youtube_recording(match_database_id:int,video_id:str,youtube_url:str):
    db=await connect()
    try:
        await db.execute("UPDATE season_matches SET youtube_video_id=?,youtube_url=?,recording_status='ready' WHERE id=?",(video_id,youtube_url,match_database_id)); await db.commit()
    finally: await db.close()
