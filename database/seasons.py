from datetime import datetime, timezone
import json

from database.connection import connect


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _guild_id_text(guild_id: int) -> str:
    if guild_id <= 0:
        raise ValueError("guild_id must be a positive integer.")
    return str(guild_id)


async def get_active_season(
    guild_id: int,
):
    database = await connect()

    try:
        cursor = await database.execute(
            """
            SELECT
                id,
                guild_id,
                season_number,
                required_prefix,
                tracked_player_count,
                started_at,
                ended_at,
                is_active
            FROM community_seasons
            WHERE guild_id = ?
              AND is_active = 1
            LIMIT 1
            """,
            (_guild_id_text(guild_id),),
        )

        return await cursor.fetchone()
    finally:
        await database.close()


async def get_season_by_number(
    guild_id: int,
    season_number: int,
):
    database = await connect()

    try:
        cursor = await database.execute(
            """
            SELECT
                id,
                guild_id,
                season_number,
                required_prefix,
                tracked_player_count,
                started_at,
                ended_at,
                is_active
            FROM community_seasons
            WHERE guild_id = ?
              AND season_number = ?
            LIMIT 1
            """,
            (
                _guild_id_text(guild_id),
                season_number,
            ),
        )

        return await cursor.fetchone()
    finally:
        await database.close()


async def get_season_by_prefix(
    guild_id: int,
    required_prefix: str,
):
    database = await connect()
    try:
        cursor = await database.execute(
            """
            SELECT
                id, guild_id, season_number, required_prefix,
                tracked_player_count, started_at, ended_at, is_active
            FROM community_seasons
            WHERE guild_id = ?
              AND required_prefix = ? COLLATE NOCASE
            LIMIT 1
            """,
            (_guild_id_text(guild_id), required_prefix.strip()),
        )
        return await cursor.fetchone()
    finally:
        await database.close()


async def get_latest_season(
    guild_id: int,
):
    database = await connect()

    try:
        cursor = await database.execute(
            """
            SELECT
                id,
                guild_id,
                season_number,
                required_prefix,
                tracked_player_count,
                started_at,
                ended_at,
                is_active
            FROM community_seasons
            WHERE guild_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (_guild_id_text(guild_id),),
        )

        return await cursor.fetchone()
    finally:
        await database.close()


async def create_season(
    guild_id: int,
    required_prefix: str,
):
    """
    Create or resume a community season for one Discord guild.

    Prefixes are case-insensitive public identifiers. The existing
    season_number column remains a stable internal numeric scope key so
    archived account links and existing season data stay compatible.
    """
    canonical_prefix = required_prefix.strip()

    if (
        not canonical_prefix
        or len(canonical_prefix) > 20
        or not canonical_prefix.isalnum()
    ):
        raise ValueError(
            "Season prefix must be 1-20 alphanumeric characters."
        )

    guild_text = _guild_id_text(guild_id)
    active_season = await get_active_season(guild_id)

    if active_season is not None:
        raise RuntimeError("End the current season first.")

    database = await connect()

    try:
        existing_cursor = await database.execute(
            """
            SELECT
                id, guild_id, season_number, required_prefix,
                tracked_player_count, started_at, ended_at, is_active
            FROM community_seasons
            WHERE guild_id = ?
              AND required_prefix = ? COLLATE NOCASE
            LIMIT 1
            """,
            (guild_text, canonical_prefix),
        )
        existing_season = await existing_cursor.fetchone()

        latest_cursor = await database.execute(
            """
            SELECT id, season_number, required_prefix
            FROM community_seasons
            WHERE guild_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (guild_text,),
        )
        latest_season = await latest_cursor.fetchone()

        if existing_season is not None:
            if (
                latest_season is not None
                and latest_season["id"] != existing_season["id"]
            ):
                raise RuntimeError(
                    (
                        f"Season `{existing_season['required_prefix']}` is "
                        "archived and cannot be resumed because a newer "
                        "season has already begun in this server."
                    )
                )

            await database.execute(
                """
                UPDATE community_seasons
                SET is_active = 1,
                    ended_at = NULL
                WHERE id = ?
                  AND guild_id = ?
                """,
                (existing_season["id"], guild_text),
            )
            await database.commit()

            reopened_cursor = await database.execute(
                """
                SELECT
                    id, guild_id, season_number, required_prefix,
                    tracked_player_count, started_at, ended_at, is_active
                FROM community_seasons
                WHERE id = ?
                  AND guild_id = ?
                """,
                (existing_season["id"], guild_text),
            )
            return await reopened_cursor.fetchone()

        next_cursor = await database.execute(
            """
            SELECT COALESCE(MAX(season_number), 0) + 1 AS next_number
            FROM community_seasons
            WHERE guild_id = ?
            """,
            (guild_text,),
        )
        next_row = await next_cursor.fetchone()
        internal_number = int(next_row["next_number"])

        cursor = await database.execute(
            """
            INSERT INTO community_seasons (
                guild_id,
                season_number,
                required_prefix,
                tracked_player_count,
                started_at,
                ended_at,
                is_active
            )
            VALUES (?, ?, ?, 10, ?, NULL, 1)
            """,
            (
                guild_text,
                internal_number,
                canonical_prefix,
                utc_now(),
            ),
        )
        await database.commit()

        created_cursor = await database.execute(
            """
            SELECT
                id, guild_id, season_number, required_prefix,
                tracked_player_count, started_at, ended_at, is_active
            FROM community_seasons
            WHERE id = ?
              AND guild_id = ?
            """,
            (cursor.lastrowid, guild_text),
        )
        return await created_cursor.fetchone()
    finally:
        await database.close()


async def end_active_season(
    guild_id: int,
):
    active_season = await get_active_season(guild_id)

    if active_season is None:
        return None

    database = await connect()

    try:
        now = utc_now()
        guild_text = _guild_id_text(guild_id)

        await database.execute(
            """
            UPDATE community_seasons
            SET
                is_active = 0,
                ended_at = ?
            WHERE id = ?
              AND guild_id = ?
            """,
            (
                now,
                active_season["id"],
                guild_text,
            ),
        )

        # Season-scoped automatic posting must never outlive the season.
        await database.execute(
            """
            UPDATE daily_leaderboard_configs
            SET enabled = 0, updated_at = ?
            WHERE guild_id = ?
            """,
            (now, guild_text),
        )
        await database.execute(
            """
            UPDATE season_match_configs
            SET enabled = 0, updated_at = ?
            WHERE guild_id = ?
            """,
            (now, guild_text),
        )

        await database.commit()
        return active_season
    finally:
        await database.close()


async def is_season_player_voided(
    community_season_id: int,
    proavalon_username: str,
) -> bool:
    database = await connect()

    try:
        cursor = await database.execute(
            """
            SELECT 1
            FROM season_voided_players
            WHERE community_season_id = ?
              AND proavalon_username = ? COLLATE NOCASE
            LIMIT 1
            """,
            (
                community_season_id,
                proavalon_username.strip(),
            ),
        )

        return await cursor.fetchone() is not None

    finally:
        await database.close()


async def void_season_player(
    community_season_id: int,
    proavalon_username: str,
    voided_by_discord_user_id: int,
) -> bool:
    """
    Exclude a username from one community season.

    The username is also removed from season_players immediately so it
    disappears from registered-player lists, comparisons, leaderboards,
    and daily leaderboard snapshots.

    Returns True if a new void record was created, or False if the
    username was already voided.
    """

    canonical_username = proavalon_username.strip()

    if not canonical_username:
        raise ValueError(
            "ProAvalon username cannot be empty."
        )

    database = await connect()

    try:
        await database.execute("BEGIN")

        existing_cursor = await database.execute(
            """
            SELECT 1
            FROM season_voided_players
            WHERE community_season_id = ?
              AND proavalon_username = ? COLLATE NOCASE
            LIMIT 1
            """,
            (
                community_season_id,
                canonical_username,
            ),
        )

        already_voided = (
            await existing_cursor.fetchone()
            is not None
        )

        if not already_voided:
            await database.execute(
                """
                INSERT INTO season_voided_players (
                    community_season_id,
                    proavalon_username,
                    voided_by_discord_user_id,
                    voided_at
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    community_season_id,
                    canonical_username,
                    str(voided_by_discord_user_id),
                    utc_now(),
                ),
            )

        # Remove it from the live season roster even if it had already
        # registered previously.
        await database.execute(
            """
            DELETE FROM season_players
            WHERE community_season_id = ?
              AND proavalon_username = ? COLLATE NOCASE
            """,
            (
                community_season_id,
                canonical_username,
            ),
        )

        await database.commit()

        return not already_voided

    except Exception:
        await database.rollback()
        raise

    finally:
        await database.close()


async def unvoid_season_player(
    community_season_id: int,
    proavalon_username: str,
):
    """
    Remove a username from the season void list.

    This does not automatically re-register the player. The account may
    register normally again the next time it is looked up with /stats
    player and satisfies the season requirements.
    """

    database = await connect()

    try:
        cursor = await database.execute(
            """
            SELECT
                id,
                community_season_id,
                proavalon_username,
                voided_by_discord_user_id,
                voided_at
            FROM season_voided_players
            WHERE community_season_id = ?
              AND proavalon_username = ? COLLATE NOCASE
            LIMIT 1
            """,
            (
                community_season_id,
                proavalon_username.strip(),
            ),
        )

        row = await cursor.fetchone()

        if row is None:
            return None

        await database.execute(
            """
            DELETE FROM season_voided_players
            WHERE id = ?
            """,
            (row["id"],),
        )

        await database.commit()
        return row

    finally:
        await database.close()


async def get_voided_season_players(
    community_season_id: int,
):
    database = await connect()

    try:
        cursor = await database.execute(
            """
            SELECT
                id,
                community_season_id,
                proavalon_username,
                voided_by_discord_user_id,
                voided_at
            FROM season_voided_players
            WHERE community_season_id = ?
            ORDER BY proavalon_username COLLATE NOCASE ASC
            """,
            (community_season_id,),
        )

        return await cursor.fetchall()

    finally:
        await database.close()


async def count_voided_season_players(
    community_season_id: int,
) -> int:
    database = await connect()

    try:
        cursor = await database.execute(
            """
            SELECT COUNT(*) AS total
            FROM season_voided_players
            WHERE community_season_id = ?
            """,
            (community_season_id,),
        )

        row = await cursor.fetchone()
        return int(row["total"])

    finally:
        await database.close()


async def register_season_player(
    community_season_id: int,
    proavalon_username: str,
) -> bool:
    """
    Register a player unless that username has been explicitly voided
    from this season.

    Returns True when the username is allowed in the season and False
    when it is currently voided.
    """

    canonical_username = proavalon_username.strip()

    if not canonical_username:
        raise ValueError(
            "ProAvalon username cannot be empty."
        )

    if await is_season_player_voided(
        community_season_id,
        canonical_username,
    ):
        return False

    timestamp = utc_now()
    database = await connect()

    try:
        await database.execute(
            """
            INSERT INTO season_players (
                community_season_id,
                proavalon_username,
                first_seen_at,
                last_seen_at
            )
            VALUES (?, ?, ?, ?)

            ON CONFLICT (
                community_season_id,
                proavalon_username
            )
            DO UPDATE SET
                proavalon_username =
                    excluded.proavalon_username,
                last_seen_at =
                    excluded.last_seen_at
            """,
            (
                community_season_id,
                canonical_username,
                timestamp,
                timestamp,
            ),
        )

        await database.commit()
        return True

    finally:
        await database.close()


async def count_season_players(
    community_season_id: int,
) -> int:
    database = await connect()

    try:
        cursor = await database.execute(
            """
            SELECT COUNT(*) AS total
            FROM season_players
            WHERE community_season_id = ?
            """,
            (community_season_id,),
        )

        row = await cursor.fetchone()
        return int(row["total"])
    finally:
        await database.close()


async def get_season_players(
    community_season_id: int,
):
    database = await connect()

    try:
        cursor = await database.execute(
            """
            SELECT
                id,
                community_season_id,
                proavalon_username,
                first_seen_at,
                last_seen_at
            FROM season_players
            WHERE community_season_id = ?
            ORDER BY proavalon_username COLLATE NOCASE ASC
            """,
            (community_season_id,),
        )

        return await cursor.fetchall()
    finally:
        await database.close()


async def reset_all_season_data(
    guild_id: int,
) -> None:
    """
    Permanently delete all season history for one Discord guild only.

    Other guilds, their seasons, account links, and bot administrators
    are not affected.
    """

    guild_text = _guild_id_text(guild_id)
    database = await connect()

    try:
        await database.execute("BEGIN")

        season_cursor = await database.execute(
            """
            SELECT id
            FROM community_seasons
            WHERE guild_id = ?
            """,
            (guild_text,),
        )
        season_rows = await season_cursor.fetchall()
        season_ids = [row["id"] for row in season_rows]

        for season_id in season_ids:
            await database.execute(
                """
                DELETE FROM season_players
                WHERE community_season_id = ?
                """,
                (season_id,),
            )

        # Disable season-scoped scheduled output before deleting season history.
        now = utc_now()
        await database.execute(
            """
            UPDATE daily_leaderboard_configs
            SET enabled = 0, updated_at = ?
            WHERE guild_id = ?
            """,
            (now, guild_text),
        )
        await database.execute(
            """
            UPDATE season_match_configs
            SET enabled = 0, updated_at = ?
            WHERE guild_id = ?
            """,
            (now, guild_text),
        )

        await database.execute(
            """
            DELETE FROM community_seasons
            WHERE guild_id = ?
            """,
            (guild_text,),
        )

        await database.commit()

    except Exception:
        await database.rollback()
        raise

    finally:
        await database.close()


async def get_all_active_seasons():
    database = await connect()
    try:
        cursor = await database.execute("""SELECT id,guild_id,season_number,required_prefix,tracked_player_count,started_at,ended_at,is_active FROM community_seasons WHERE is_active=1""")
        return await cursor.fetchall()
    finally:
        await database.close()

async def fetch_season_history(
    guild_id: int,
    required_prefix: str | None = None,
    source_guild_id: int | None = None,
) -> dict:
    """
    Backfill one guild's existing season from the same-prefix season in
    another guild.

    Historical match rows and registered players are copied. Discord/server
    configuration, account links, void lists, role settings, and posting
    state are intentionally not copied.

    The operation is idempotent because season_matches is unique on
    (community_season_id, match_fingerprint).
    """
    guild_text = _guild_id_text(guild_id)
    database = await connect()

    try:
        await database.execute("BEGIN")

        destination_cursor = await database.execute(
            """
            SELECT *
            FROM community_seasons
            WHERE guild_id = ?
              AND is_active = 1
            LIMIT 1
            """,
            (guild_text,),
        )
        destination = await destination_cursor.fetchone()

        if destination is None:
            raise RuntimeError(
                "Create/set the destination season in this server first."
            )

        destination_prefix = str(destination["required_prefix"])
        requested_prefix = (
            required_prefix.strip()
            if required_prefix is not None
            else destination_prefix
        )

        if not requested_prefix:
            raise ValueError("Season prefix cannot be empty.")

        if requested_prefix.casefold() != destination_prefix.casefold():
            raise RuntimeError(
                f"This server's active season uses prefix "
                f"`{destination_prefix}`, not `{requested_prefix}`."
            )

        params: list[object] = [
            guild_text,
            requested_prefix,
        ]
        source_filter = ""

        if source_guild_id is not None:
            source_text = _guild_id_text(source_guild_id)
            if source_text == guild_text:
                raise RuntimeError(
                    "The source server must be different from this server."
                )
            source_filter = " AND guild_id = ?"
            params.append(source_text)

        source_cursor = await database.execute(
            f"""
            SELECT
                cs.*,
                (
                    SELECT COUNT(*)
                    FROM season_matches sm
                    WHERE sm.community_season_id = cs.id
                ) AS archived_match_count
            FROM community_seasons cs
            WHERE guild_id <> ?
              AND required_prefix = ? COLLATE NOCASE
              {source_filter}
            ORDER BY archived_match_count DESC, id DESC
            """,
            tuple(params),
        )
        candidates = await source_cursor.fetchall()

        if not candidates:
            raise RuntimeError(
                f"No `{requested_prefix}` season was found in another server."
            )

        if source_guild_id is None and len(candidates) > 1:
            # If exactly one candidate actually has history, it is unambiguous.
            candidates_with_history = [
                row for row in candidates
                if int(row["archived_match_count"] or 0) > 0
            ]
            if len(candidates_with_history) == 1:
                source = candidates_with_history[0]
            else:
                candidate_ids = ", ".join(
                    str(row["guild_id"]) for row in candidates
                )
                raise RuntimeError(
                    "Multiple source seasons use that prefix. "
                    "Run the command again with `source_server_id`. "
                    f"Candidate server IDs: {candidate_ids}"
                )
        else:
            source = candidates[0]

        source_id = int(source["id"])
        destination_id = int(destination["id"])

        # Read the live schema so this remains compatible with newer archived
        # match columns (season_match_id, timing/card fields, etc.) without
        # hard-coding an old column list.
        pragma_cursor = await database.execute(
            "PRAGMA table_info(season_matches)"
        )
        table_info = await pragma_cursor.fetchall()
        all_columns = [str(row["name"]) for row in table_info]

        copy_columns = [
            column for column in all_columns
            if column not in {
                "id",
                "community_season_id",
                # Posting is guild/channel-specific. A copied match should not
                # claim it was posted in the destination server.
                "posted_at",
            }
        ]

        if "match_fingerprint" not in copy_columns:
            raise RuntimeError(
                "season_matches is missing match_fingerprint."
            )

        match_cursor = await database.execute(
            """
            SELECT *
            FROM season_matches
            WHERE community_season_id = ?
            ORDER BY finished_at ASC, id ASC
            """,
            (source_id,),
        )
        source_matches = await match_cursor.fetchall()

        copied = 0
        skipped = 0
        copied_usernames: dict[str, str] = {}

        for row in source_matches:
            # Preserve every archived field supported by the current schema.
            values = [row[column] for column in copy_columns]

            placeholders = ", ".join("?" for _ in copy_columns)
            columns_sql = ", ".join(copy_columns)

            cursor = await database.execute(
                f"""
                INSERT OR IGNORE INTO season_matches (
                    community_season_id,
                    {columns_sql}
                )
                VALUES (?, {placeholders})
                """,
                (destination_id, *values),
            )

            if cursor.rowcount == 1:
                copied += 1
            else:
                skipped += 1

            # Also reconstruct the destination roster from archived matches.
            if "players_json" in all_columns:
                raw_players = row["players_json"]
                try:
                    players = (
                        raw_players
                        if isinstance(raw_players, list)
                        else json.loads(raw_players or "[]")
                    )
                except (TypeError, ValueError, json.JSONDecodeError):
                    players = []

                if isinstance(players, list):
                    for username in players:
                        if isinstance(username, str) and username.strip():
                            copied_usernames[
                                username.strip().casefold()
                            ] = username.strip()

        # Copy the source registered-player roster too. This includes linked
        # zero-game players that were registered in the source season.
        player_cursor = await database.execute(
            """
            SELECT proavalon_username, first_seen_at, last_seen_at
            FROM season_players
            WHERE community_season_id = ?
            """,
            (source_id,),
        )
        source_players = await player_cursor.fetchall()

        players_added = 0
        for row in source_players:
            username = str(row["proavalon_username"]).strip()
            if not username:
                continue
            copied_usernames[username.casefold()] = username

            cursor = await database.execute(
                """
                INSERT OR IGNORE INTO season_players (
                    community_season_id,
                    proavalon_username,
                    first_seen_at,
                    last_seen_at
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    destination_id,
                    username,
                    row["first_seen_at"],
                    row["last_seen_at"],
                ),
            )
            if cursor.rowcount == 1:
                players_added += 1

        # Add any players found in match history but absent from source roster.
        now = utc_now()
        for username in copied_usernames.values():
            cursor = await database.execute(
                """
                INSERT OR IGNORE INTO season_players (
                    community_season_id,
                    proavalon_username,
                    first_seen_at,
                    last_seen_at
                )
                VALUES (?, ?, ?, ?)
                """,
                (destination_id, username, now, now),
            )
            if cursor.rowcount == 1:
                players_added += 1

        # Rebuild season-local match numbering chronologically when that newer
        # column exists. This keeps fetched Match #1, #2, ... aligned with the
        # source history and also repairs a partially fetched destination.
        if "season_match_id" in all_columns:
            destination_matches_cursor = await database.execute(
                """
                SELECT id
                FROM season_matches
                WHERE community_season_id = ?
                ORDER BY finished_at ASC, id ASC
                """,
                (destination_id,),
            )
            destination_matches = await destination_matches_cursor.fetchall()
            for index, match in enumerate(destination_matches, start=1):
                await database.execute(
                    """
                    UPDATE season_matches
                    SET season_match_id = ?
                    WHERE id = ?
                    """,
                    (index, int(match["id"])),
                )

        total_cursor = await database.execute(
            """
            SELECT COUNT(*) AS total
            FROM season_matches
            WHERE community_season_id = ?
            """,
            (destination_id,),
        )
        total_row = await total_cursor.fetchone()

        roster_cursor = await database.execute(
            """
            SELECT COUNT(*) AS total
            FROM season_players
            WHERE community_season_id = ?
            """,
            (destination_id,),
        )
        roster_row = await roster_cursor.fetchone()

        await database.commit()

        return {
            "prefix": destination_prefix,
            "source_guild_id": str(source["guild_id"]),
            "source_season_id": source_id,
            "destination_season_id": destination_id,
            "copied_matches": copied,
            "skipped_matches": skipped,
            "players_added": players_added,
            "total_matches": int(total_row["total"]),
            "total_players": int(roster_row["total"]),
        }

    except Exception:
        await database.rollback()
        raise
    finally:
        await database.close()
