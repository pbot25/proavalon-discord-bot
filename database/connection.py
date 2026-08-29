from pathlib import Path
import os

import aiosqlite

from database.schema import SCHEMA


PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Local default:
#   <project>/data/proavalon.db
#
# Cloud/Railway:
#   Set DATABASE_PATH=/data/proavalon.db
_database_path_from_env = os.getenv("DATABASE_PATH")

if _database_path_from_env:
    DATABASE_PATH = Path(_database_path_from_env).expanduser()
else:
    DATABASE_PATH = PROJECT_ROOT / "data" / "proavalon.db"


async def connect() -> aiosqlite.Connection:
    DATABASE_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    database = await aiosqlite.connect(DATABASE_PATH)
    database.row_factory = aiosqlite.Row

    await database.execute(
        "PRAGMA foreign_keys = ON;"
    )

    return database


async def _table_columns(
    database: aiosqlite.Connection,
    table_name: str,
) -> set[str]:
    cursor = await database.execute(
        f"PRAGMA table_info({table_name})"
    )
    rows = await cursor.fetchall()
    return {str(row["name"]) for row in rows}


async def _table_exists(
    database: aiosqlite.Connection,
    table_name: str,
) -> bool:
    cursor = await database.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table'
          AND name = ?
        LIMIT 1
        """,
        (table_name,),
    )
    return await cursor.fetchone() is not None


async def _migrate_global_config_to_guild_scoped(
    database: aiosqlite.Connection,
) -> None:
    """
    Migrate the old global configuration schema to the new guild-scoped
    schema.

    The old rows cannot be assigned safely to a Discord guild because the
    previous schema never stored a guild ID. They are preserved in
    legacy_* tables instead of being silently attached to the wrong server.

    After migration, each server starts with its own empty configuration.
    Discord server administrators and the application owner can bootstrap
    new bot-admin records with /admin add.
    """

    if not await _table_exists(database, "community_seasons"):
        return

    season_columns = await _table_columns(
        database,
        "community_seasons",
    )

    if "guild_id" in season_columns:
        return

    legacy_tables = (
        "legacy_community_seasons",
        "legacy_season_players",
        "legacy_account_links",
        "legacy_bot_admins",
    )

    for table_name in legacy_tables:
        if await _table_exists(database, table_name):
            raise RuntimeError(
                (
                    "A previous legacy database backup already exists "
                    f"({table_name}). Remove or archive the old legacy "
                    "tables before rerunning the guild-scope migration."
                )
            )

    await database.execute("PRAGMA foreign_keys = OFF;")

    try:
        await database.execute("BEGIN")

        # Drop old index names before renaming so the new schema may reuse
        # clean guild-scoped index names.
        old_indexes = (
            "one_active_community_season",
            "one_player_per_season",
            "one_active_discord_link",
            "one_active_proavalon_link",
            "account_links_by_discord",
            "account_links_by_username",
        )

        for index_name in old_indexes:
            await database.execute(
                f"DROP INDEX IF EXISTS {index_name}"
            )

        await database.execute(
            """
            ALTER TABLE community_seasons
            RENAME TO legacy_community_seasons
            """
        )
        await database.execute(
            """
            ALTER TABLE season_players
            RENAME TO legacy_season_players
            """
        )
        await database.execute(
            """
            ALTER TABLE account_links
            RENAME TO legacy_account_links
            """
        )
        await database.execute(
            """
            ALTER TABLE bot_admins
            RENAME TO legacy_bot_admins
            """
        )

        await database.executescript(SCHEMA)
        await database.commit()

    except Exception:
        await database.rollback()
        raise

    finally:
        await database.execute("PRAGMA foreign_keys = ON;")


async def _migrate_account_links_by_season(
    database: aiosqlite.Connection,
) -> None:
    """Add persistent per-season account links without deleting old links."""
    if not await _table_exists(database, "account_links"):
        return

    columns = await _table_columns(database, "account_links")
    if "season_number" not in columns:
        await database.execute(
            "ALTER TABLE account_links ADD COLUMN season_number INTEGER"
        )

        # Best-effort migration for existing S1/S2/... style usernames.
        cursor = await database.execute(
            "SELECT id, proavalon_username FROM account_links"
        )
        rows = await cursor.fetchall()
        import re

        for row in rows:
            username = str(row["proavalon_username"] or "")
            match = re.match(r"^[sS](\d+)", username)
            if match:
                await database.execute(
                    "UPDATE account_links SET season_number = ? WHERE id = ?",
                    (int(match.group(1)), row["id"]),
                )

    # Replace the old one-link-per-guild uniqueness with per-season uniqueness.
    await database.execute(
        "DROP INDEX IF EXISTS one_active_discord_link_per_guild"
    )
    await database.execute(
        "DROP INDEX IF EXISTS one_active_proavalon_link_per_guild"
    )


async def _migrate_daily_leaderboard_types(
    database: aiosqlite.Connection,
) -> None:
    """Allow the newer Resistance/Spy daily leaderboard types on old DBs."""
    if not await _table_exists(database, "daily_leaderboard_configs"):
        return

    cursor = await database.execute(
        """
        SELECT sql
        FROM sqlite_master
        WHERE type='table'
          AND name='daily_leaderboard_configs'
        """
    )
    row = await cursor.fetchone()
    sql = str(row["sql"] or "") if row else ""

    if "'resistance'" in sql and "'spy'" in sql:
        return

    await database.execute(
        """
        ALTER TABLE daily_leaderboard_configs
        RENAME TO daily_leaderboard_configs_old
        """
    )
    await database.execute(
        """
        CREATE TABLE daily_leaderboard_configs (
            guild_id TEXT PRIMARY KEY,
            leaderboard_type TEXT NOT NULL
                CHECK (
                    leaderboard_type IN (
                        'overall',
                        'resistance',
                        'spy',
                        'rating'
                    )
                ),
            channel_id TEXT NOT NULL,
            post_time TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1
                CHECK(enabled IN(0,1)),
            minimum_games INTEGER NOT NULL DEFAULT 5,
            top_count INTEGER NOT NULL DEFAULT 15,
            last_post_local_date TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    await database.execute(
        """
        INSERT INTO daily_leaderboard_configs
        SELECT
            guild_id,
            leaderboard_type,
            channel_id,
            post_time,
            enabled,
            minimum_games,
            top_count,
            last_post_local_date,
            updated_at
        FROM daily_leaderboard_configs_old
        """
    )
    await database.execute(
        "DROP TABLE daily_leaderboard_configs_old"
    )



async def _migrate_season_display_names(
    database: aiosqlite.Connection,
) -> None:
    """Add the public season display name without resetting old data."""
    if not await _table_exists(database, "community_seasons"):
        return

    columns = await _table_columns(database, "community_seasons")
    if "display_name" not in columns:
        await database.execute(
            "ALTER TABLE community_seasons ADD COLUMN display_name TEXT"
        )

    # Preserve the old visible label for existing seasons until an admin
    # explicitly renames them. Newly created seasons always require a name.
    await database.execute(
        """
        UPDATE community_seasons
        SET display_name = 'Community Season ' || season_number
        WHERE display_name IS NULL
           OR TRIM(display_name) = ''
        """
    )


async def _repair_account_link_season_scopes(
    database: aiosqlite.Connection,
) -> None:
    """
    Repair links saved under a prefix-derived number (for example S4 -> 4)
    when season_number is actually an internal per-guild scope key.

    The target season is selected from the guild's configured prefixes,
    preferring the longest matching prefix.
    """
    if not await _table_exists(database, "account_links"):
        return
    if not await _table_exists(database, "community_seasons"):
        return

    seasons_cursor = await database.execute(
        """
        SELECT id, guild_id, season_number, required_prefix
        FROM community_seasons
        ORDER BY guild_id ASC, LENGTH(required_prefix) DESC, id DESC
        """
    )
    season_rows = await seasons_cursor.fetchall()

    seasons_by_guild: dict[str, list] = {}
    for row in season_rows:
        seasons_by_guild.setdefault(str(row["guild_id"]), []).append(row)

    links_cursor = await database.execute(
        """
        SELECT *
        FROM account_links
        WHERE is_active = 1
        ORDER BY id ASC
        """
    )
    links = await links_cursor.fetchall()

    for link in links:
        guild_id = str(link["guild_id"])
        username = str(link["proavalon_username"] or "").strip()
        if not username:
            continue

        target = None
        lowered = username.casefold()
        for season in seasons_by_guild.get(guild_id, []):
            prefix = str(season["required_prefix"] or "").strip()
            if prefix and lowered.startswith(prefix.casefold()):
                target = season
                break

        if target is None:
            continue

        target_number = int(target["season_number"])
        current_number = link["season_number"]
        if current_number is not None and int(current_number) == target_number:
            continue

        # Do not create an ambiguous active collision. If an identical link
        # already exists at the correct scope, retire this legacy duplicate.
        conflict_cursor = await database.execute(
            """
            SELECT *
            FROM account_links
            WHERE guild_id = ?
              AND is_active = 1
              AND id <> ?
              AND season_number = ?
              AND (
                    discord_user_id = ?
                    OR proavalon_username = ? COLLATE NOCASE
                  )
            ORDER BY id DESC
            LIMIT 1
            """,
            (
                guild_id,
                int(link["id"]),
                target_number,
                str(link["discord_user_id"]),
                username,
            ),
        )
        conflict = await conflict_cursor.fetchone()

        if conflict is not None:
            same_pair = (
                str(conflict["discord_user_id"]) == str(link["discord_user_id"])
                and str(conflict["proavalon_username"]).casefold()
                == username.casefold()
            )
            if same_pair:
                await database.execute(
                    """
                    UPDATE account_links
                    SET is_active = 0, unlinked_at = COALESCE(unlinked_at, linked_at)
                    WHERE id = ?
                    """,
                    (int(link["id"]),),
                )
            # If it is a real conflicting mapping, leave it untouched rather
            # than guessing which user/account should win.
            continue

        await database.execute(
            """
            UPDATE account_links
            SET season_number = ?
            WHERE id = ?
            """,
            (target_number, int(link["id"])),
        )


async def _migrate_season_match_ids(
    database: aiosqlite.Connection,
) -> None:
    """
    Give every archived match a season-local public match ID.

    IDs are 1, 2, 3... independently inside each community season.
    Existing live/archived seasons are backfilled on deployment in their
    historical finish order, so the feature applies immediately.
    """
    if not await _table_exists(database, "season_matches"):
        return

    columns = await _table_columns(database, "season_matches")
    if "season_match_id" not in columns:
        await database.execute(
            "ALTER TABLE season_matches ADD COLUMN season_match_id INTEGER"
        )

    cursor = await database.execute(
        """
        SELECT id, community_season_id, season_match_id
        FROM season_matches
        ORDER BY community_season_id ASC, finished_at ASC, id ASC
        """
    )
    rows = await cursor.fetchall()

    next_by_season: dict[int, int] = {}
    for row in rows:
        season_id = int(row["community_season_id"])
        next_id = next_by_season.get(season_id, 1)

        # Re-number all rows deterministically. This guarantees a clean,
        # gap-free 1..N sequence for pre-existing seasons.
        await database.execute(
            """
            UPDATE season_matches
            SET season_match_id = ?
            WHERE id = ?
            """,
            (next_id, int(row["id"])),
        )
        next_by_season[season_id] = next_id + 1

    await database.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS one_match_number_per_season
        ON season_matches(community_season_id, season_match_id)
        """
    )


async def initialize_database() -> None:
    database = await connect()

    try:
        await _migrate_global_config_to_guild_scoped(
            database
        )
        await _migrate_daily_leaderboard_types(database)
        await _migrate_account_links_by_season(database)
        await _migrate_season_display_names(database)
        await database.executescript(SCHEMA)
        await _repair_account_link_season_scopes(database)
        await _migrate_season_match_ids(database)
        await database.commit()
    finally:
        await database.close()