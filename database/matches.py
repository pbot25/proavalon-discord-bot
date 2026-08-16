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

async def save_match(*, season_id:int, fingerprint:str, room_id:int, detected_at:str, winner, game_mode, mission_history, fails_history, vote_history, players, roles, rating_changes):
    db=await connect()
    try:
        c=await db.execute("""INSERT OR IGNORE INTO season_matches(community_season_id,match_fingerprint,room_id,detected_at,finished_at,winner,game_mode,mission_history_json,fails_history_json,vote_history_json,players_json,roles_json,rating_changes_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",(season_id,fingerprint,room_id,detected_at,utc_now(),winner,game_mode,json.dumps(mission_history),json.dumps(fails_history),json.dumps(vote_history) if vote_history is not None else None,json.dumps(players),json.dumps(roles) if roles is not None else None,json.dumps(rating_changes)))
        await db.commit(); return c.rowcount>0
    finally: await db.close()

async def mark_match_posted(season_id:int, fingerprint:str):
    db=await connect()
    try:
        await db.execute("UPDATE season_matches SET posted_at=? WHERE community_season_id=? AND match_fingerprint=?",(utc_now(),season_id,fingerprint)); await db.commit()
    finally: await db.close()
