import json
from dataclasses import asdict
from datetime import datetime, timezone

from database.connection import connect
from proavalon.profile_scraper import PlayerProfile, RoleRecord


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _deserialize_profile(raw: str) -> PlayerProfile:
    data = json.loads(raw)
    role_stats = {
        game_size: {
            role: RoleRecord(
                wins=int(record.get("wins", 0)),
                losses=int(record.get("losses", 0)),
            )
            for role, record in records.items()
        }
        for game_size, records in (data.get("role_stats") or {}).items()
    }
    data["role_stats"] = role_stats
    return PlayerProfile(**data)


async def save_profile_snapshot(profile: PlayerProfile) -> None:
    database = await connect()
    try:
        await database.execute(
            """
            INSERT INTO proavalon_profile_snapshots(
                username_key, username, profile_json, fetched_at
            ) VALUES(?, ?, ?, ?)
            ON CONFLICT(username_key) DO UPDATE SET
                username = excluded.username,
                profile_json = excluded.profile_json,
                fetched_at = excluded.fetched_at
            """,
            (
                profile.username.strip().casefold(),
                profile.username,
                json.dumps(asdict(profile), separators=(",", ":")),
                utc_now(),
            ),
        )
        await database.commit()
    finally:
        await database.close()


async def load_profile_snapshots(
    usernames: set[str],
) -> tuple[list[PlayerProfile], int | None]:
    keys = sorted(
        username.strip().casefold()
        for username in usernames
        if username.strip()
    )
    if not keys:
        return [], None

    placeholders = ",".join("?" for _ in keys)
    database = await connect()
    try:
        cursor = await database.execute(
            f"""
            SELECT profile_json, fetched_at
            FROM proavalon_profile_snapshots
            WHERE username_key IN ({placeholders})
            """,
            keys,
        )
        rows = await cursor.fetchall()
    finally:
        await database.close()

    profiles: list[PlayerProfile] = []
    fetched_times: list[datetime] = []
    for row in rows:
        try:
            profiles.append(_deserialize_profile(str(row["profile_json"])))
            fetched = datetime.fromisoformat(
                str(row["fetched_at"]).replace("Z", "+00:00")
            )
            if fetched.tzinfo is None:
                fetched = fetched.replace(tzinfo=timezone.utc)
            fetched_times.append(fetched.astimezone(timezone.utc))
        except (TypeError, ValueError, KeyError, json.JSONDecodeError):
            continue

    age_seconds = None
    if fetched_times:
        age_seconds = max(
            0,
            int((datetime.now(timezone.utc) - min(fetched_times)).total_seconds()),
        )
    return profiles, age_seconds
