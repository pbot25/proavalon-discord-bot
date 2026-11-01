from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import zipfile

from database.connection import connect


ARCHIVE_TABLES = (
    "community_seasons",
    "season_players",
    "season_voided_players",
    "season_matches",
    "account_links",
)


@dataclass(frozen=True)
class SeasonArchive:
    path: Path
    filename: str
    guild_id: int
    season_number: int
    season_name: str
    snapshot_sha256: str
    archive_sha256: str
    row_counts: dict[str, int]


@dataclass(frozen=True)
class UnloadResult:
    season_name: str
    season_number: int
    row_counts: dict[str, int]
    compacted: bool


@dataclass(frozen=True)
class PreparedSeasonArchive:
    filename: str
    archive_sha256: str
    snapshot_sha256: str
    manifest: dict
    rows_by_table: dict[str, list[dict]]


@dataclass(frozen=True)
class MountResult:
    season_name: str
    season_number: int
    row_counts: dict[str, int]


@dataclass(frozen=True)
class UnmountResult:
    season_name: str
    season_number: int
    row_counts: dict[str, int]
    compacted: bool


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_dict(row) -> dict:
    return {key: row[key] for key in row.keys()}


async def _season_snapshot(database, guild_id: int) -> tuple[dict, dict[str, list[dict]]]:
    guild_text = str(guild_id)
    cursor = await database.execute(
        """
        SELECT *
        FROM community_seasons
        WHERE guild_id = ?
        ORDER BY is_active DESC, id DESC
        LIMIT 1
        """,
        (guild_text,),
    )
    season_row = await cursor.fetchone()
    if season_row is None:
        raise RuntimeError("This server has no community season.")

    season = _row_dict(season_row)
    season_id = int(season["id"])
    season_number = int(season["season_number"])

    queries = {
        "community_seasons": (
            "SELECT * FROM community_seasons WHERE id = ? ORDER BY id",
            (season_id,),
        ),
        "season_players": (
            "SELECT * FROM season_players WHERE community_season_id = ? ORDER BY id",
            (season_id,),
        ),
        "season_voided_players": (
            "SELECT * FROM season_voided_players WHERE community_season_id = ? ORDER BY id",
            (season_id,),
        ),
        "season_matches": (
            "SELECT * FROM season_matches WHERE community_season_id = ? ORDER BY id",
            (season_id,),
        ),
        "account_links": (
            """
            SELECT * FROM account_links
            WHERE guild_id = ? AND season_number = ?
            ORDER BY id
            """,
            (guild_text, season_number),
        ),
    }

    rows_by_table: dict[str, list[dict]] = {}
    for table_name in ARCHIVE_TABLES:
        sql, parameters = queries[table_name]
        table_cursor = await database.execute(sql, parameters)
        rows_by_table[table_name] = [
            _row_dict(row) for row in await table_cursor.fetchall()
        ]

    return season, rows_by_table


def _snapshot_sha256(rows_by_table: dict[str, list[dict]]) -> str:
    digest = hashlib.sha256()
    for table_name in ARCHIVE_TABLES:
        digest.update(table_name.encode("utf-8"))
        digest.update(b"\0")
        for row in rows_by_table[table_name]:
            encoded = json.dumps(
                row,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            digest.update(encoded)
            digest.update(b"\n")
    return digest.hexdigest()


def _safe_filename_part(value: str) -> str:
    cleaned = "".join(
        character if character.isalnum() or character in "-_" else "-"
        for character in value.strip()
    ).strip("-")
    return cleaned[:60] or "season"


def _write_archive(
    *,
    table_sql: dict[str, str],
    rows_by_table: dict[str, list[dict]],
    manifest: dict,
    output_path: Path,
) -> None:
    with tempfile.TemporaryDirectory(prefix="proavalon-season-") as temporary:
        temporary_path = Path(temporary)
        database_path = temporary_path / "season.sqlite3"
        manifest_path = temporary_path / "manifest.json"

        archive_database = sqlite3.connect(database_path)
        try:
            archive_database.execute("PRAGMA foreign_keys = ON")
            for table_name in ARCHIVE_TABLES:
                archive_database.execute(table_sql[table_name])

            for table_name in ARCHIVE_TABLES:
                rows = rows_by_table[table_name]
                if not rows:
                    continue
                columns = list(rows[0].keys())
                placeholders = ", ".join("?" for _ in columns)
                column_sql = ", ".join(columns)
                archive_database.executemany(
                    f"INSERT INTO {table_name} ({column_sql}) VALUES ({placeholders})",
                    [tuple(row[column] for column in columns) for row in rows],
                )

            archive_database.execute(
                """
                CREATE TABLE archive_manifest (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            archive_database.executemany(
                "INSERT INTO archive_manifest (key, value) VALUES (?, ?)",
                [
                    (key, json.dumps(value, ensure_ascii=False, sort_keys=True))
                    for key, value in manifest.items()
                ],
            )
            archive_database.commit()
        finally:
            archive_database.close()

        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

        with zipfile.ZipFile(
            output_path,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            archive.write(database_path, arcname="season.sqlite3")
            archive.write(manifest_path, arcname="manifest.json")


async def build_current_season_archive(guild_id: int) -> SeasonArchive:
    database = await connect()
    try:
        await database.execute("BEGIN")
        season, rows_by_table = await _season_snapshot(database, guild_id)

        table_sql: dict[str, str] = {}
        for table_name in ARCHIVE_TABLES:
            cursor = await database.execute(
                """
                SELECT sql FROM sqlite_master
                WHERE type = 'table' AND name = ?
                """,
                (table_name,),
            )
            row = await cursor.fetchone()
            if row is None or not row["sql"]:
                raise RuntimeError(f"Required database table `{table_name}` is missing.")
            table_sql[table_name] = str(row["sql"])

        snapshot_sha256 = _snapshot_sha256(rows_by_table)
        row_counts = {
            table_name: len(rows_by_table[table_name])
            for table_name in ARCHIVE_TABLES
        }
        season_name = str(
            season.get("display_name")
            or f"Community Season {season['season_number']}"
        )
        exported_at = _utc_now()
        manifest = {
            "format": "proavalon-season-archive",
            "format_version": 1,
            "exported_at": exported_at,
            "guild_id": str(guild_id),
            "season_number": int(season["season_number"]),
            "season_name": season_name,
            "required_prefix": season["required_prefix"],
            "snapshot_sha256": snapshot_sha256,
            "row_counts": row_counts,
            "excluded_data": [
                "recording_audit_events",
                "global_profile_and_rating_caches",
                "server_configuration",
            ],
        }
    finally:
        await database.rollback()
        await database.close()

    filename = (
        f"proavalon-{_safe_filename_part(season_name)}-"
        f"{snapshot_sha256[:12]}.db.zip"
    )
    temporary_file = tempfile.NamedTemporaryFile(
        prefix="proavalon-season-export-",
        suffix=".zip",
        delete=False,
    )
    temporary_file.close()
    output_path = Path(temporary_file.name)

    try:
        await asyncio.to_thread(
            _write_archive,
            table_sql=table_sql,
            rows_by_table=rows_by_table,
            manifest=manifest,
            output_path=output_path,
        )
        archive_sha256 = await asyncio.to_thread(
            lambda: hashlib.sha256(output_path.read_bytes()).hexdigest()
        )
    except Exception:
        output_path.unlink(missing_ok=True)
        raise

    return SeasonArchive(
        path=output_path,
        filename=filename,
        guild_id=guild_id,
        season_number=int(season["season_number"]),
        season_name=season_name,
        snapshot_sha256=snapshot_sha256,
        archive_sha256=archive_sha256,
        row_counts=row_counts,
    )


async def record_delivered_export(
    archive: SeasonArchive,
    exported_by_discord_user_id: int,
) -> None:
    database = await connect()
    try:
        now = _utc_now()
        await database.execute(
            """
            INSERT INTO season_archive_exports (
                guild_id, season_number, community_season_id,
                season_display_name, snapshot_sha256, archive_sha256,
                archive_filename, row_counts_json, exported_at,
                exported_by_discord_user_id, delivered_at, unloaded_at
            )
            SELECT
                guild_id, season_number, id, ?, ?, ?, ?, ?, ?, ?, ?, NULL
            FROM community_seasons
            WHERE guild_id = ? AND season_number = ?
            ON CONFLICT(guild_id, season_number) DO UPDATE SET
                community_season_id = excluded.community_season_id,
                season_display_name = excluded.season_display_name,
                snapshot_sha256 = excluded.snapshot_sha256,
                archive_sha256 = excluded.archive_sha256,
                archive_filename = excluded.archive_filename,
                row_counts_json = excluded.row_counts_json,
                exported_at = excluded.exported_at,
                exported_by_discord_user_id = excluded.exported_by_discord_user_id,
                delivered_at = excluded.delivered_at,
                unloaded_at = NULL
            """,
            (
                archive.season_name,
                archive.snapshot_sha256,
                archive.archive_sha256,
                archive.filename,
                json.dumps(archive.row_counts, sort_keys=True),
                now,
                str(exported_by_discord_user_id),
                now,
                str(archive.guild_id),
                archive.season_number,
            ),
        )
        await database.commit()
    finally:
        await database.close()


async def get_current_export_status(guild_id: int) -> dict | None:
    database = await connect()
    try:
        cursor = await database.execute(
            """
            SELECT archive.*
            FROM season_archive_exports archive
            JOIN (
                SELECT guild_id, season_number
                FROM community_seasons
                WHERE guild_id = ?
                ORDER BY is_active DESC, id DESC
                LIMIT 1
            ) season
              ON season.guild_id = archive.guild_id
             AND season.season_number = archive.season_number
            WHERE archive.guild_id = ?
              AND archive.delivered_at IS NOT NULL
              AND archive.unloaded_at IS NULL
            LIMIT 1
            """,
            (str(guild_id), str(guild_id)),
        )
        row = await cursor.fetchone()
        return _row_dict(row) if row is not None else None
    finally:
        await database.close()


def _read_uploaded_archive(
    archive_bytes: bytes,
    filename: str,
) -> PreparedSeasonArchive:
    archive_sha256 = hashlib.sha256(archive_bytes).hexdigest()
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as bundle:
            names = set(bundle.namelist())
            if not {"manifest.json", "season.sqlite3"}.issubset(names):
                raise RuntimeError(
                    "This is not a ProAvalon season archive: required files are missing."
                )
            database_info = bundle.getinfo("season.sqlite3")
            if database_info.file_size > 512 * 1024 * 1024:
                raise RuntimeError("The archived season database is unexpectedly large.")
            manifest = json.loads(bundle.read("manifest.json"))
            database_bytes = bundle.read("season.sqlite3")
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError) as error:
        raise RuntimeError("The uploaded season archive is invalid or damaged.") from error

    if manifest.get("format") != "proavalon-season-archive":
        raise RuntimeError("The uploaded file is not a ProAvalon season archive.")
    if int(manifest.get("format_version", 0)) != 1:
        raise RuntimeError("This season archive version is not supported.")

    with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as temporary:
        temporary.write(database_bytes)
        database_path = Path(temporary.name)

    try:
        uri = f"{database_path.resolve().as_uri()}?mode=ro"
        archived_database = sqlite3.connect(uri, uri=True)
        archived_database.row_factory = sqlite3.Row
        try:
            rows_by_table: dict[str, list[dict]] = {}
            for table_name in ARCHIVE_TABLES:
                table = archived_database.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                    (table_name,),
                ).fetchone()
                if table is None:
                    raise RuntimeError(
                        f"The archive is missing required table `{table_name}`."
                    )
                rows_by_table[table_name] = [
                    dict(row)
                    for row in archived_database.execute(
                        f"SELECT * FROM {table_name} ORDER BY id"
                    ).fetchall()
                ]
        finally:
            archived_database.close()
    finally:
        database_path.unlink(missing_ok=True)

    seasons = rows_by_table["community_seasons"]
    if len(seasons) != 1:
        raise RuntimeError("A season archive must contain exactly one season.")
    snapshot_sha256 = _snapshot_sha256(rows_by_table)
    if snapshot_sha256 != str(manifest.get("snapshot_sha256", "")):
        raise RuntimeError("The archive snapshot checksum does not match its contents.")

    expected_counts = manifest.get("row_counts", {})
    actual_counts = {
        table_name: len(rows_by_table[table_name])
        for table_name in ARCHIVE_TABLES
    }
    if any(
        int(expected_counts.get(table_name, -1)) != actual_counts[table_name]
        for table_name in ARCHIVE_TABLES
    ):
        raise RuntimeError("The archive row counts do not match its contents.")

    return PreparedSeasonArchive(
        filename=filename,
        archive_sha256=archive_sha256,
        snapshot_sha256=snapshot_sha256,
        manifest=manifest,
        rows_by_table=rows_by_table,
    )


async def prepare_season_archive_mount(
    guild_id: int,
    archive_bytes: bytes,
    filename: str,
) -> PreparedSeasonArchive:
    prepared = await asyncio.to_thread(
        _read_uploaded_archive,
        archive_bytes,
        filename,
    )
    if str(prepared.manifest.get("guild_id")) != str(guild_id):
        raise RuntimeError(
            "This archive belongs to a different Discord server and cannot be mounted here."
        )

    database = await connect()
    try:
        cursor = await database.execute(
            """
            SELECT * FROM season_archive_exports
            WHERE guild_id = ? AND season_number = ?
              AND unloaded_at IS NOT NULL
            LIMIT 1
            """,
            (str(guild_id), int(prepared.manifest["season_number"])),
        )
        exported = await cursor.fetchone()
        if exported is None:
            raise RuntimeError(
                "The bot has no completed unload record for this archive."
            )
        if str(exported["archive_sha256"]) != prepared.archive_sha256:
            raise RuntimeError(
                "The uploaded file is not the exact archive delivered before unloading."
            )
    finally:
        await database.close()
    return prepared


async def _insert_archive_rows(
    database,
    table_name: str,
    rows: list[dict],
    *,
    community_season_id: int | None = None,
) -> None:
    for row in rows:
        values = dict(row)
        values.pop("id", None)
        if community_season_id is not None:
            values["community_season_id"] = community_season_id
        columns = list(values)
        await database.execute(
            f"INSERT INTO {table_name} ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            tuple(values[column] for column in columns),
        )


async def mount_prepared_season_archive(
    guild_id: int,
    prepared: PreparedSeasonArchive,
    mounted_by_discord_user_id: int,
) -> MountResult:
    database = await connect()
    try:
        await database.execute("BEGIN IMMEDIATE")
        active_cursor = await database.execute(
            "SELECT 1 FROM community_seasons WHERE guild_id = ? AND is_active = 1",
            (str(guild_id),),
        )
        if await active_cursor.fetchone() is not None:
            raise RuntimeError(
                "End or unload the active season before mounting an archive."
            )
        mount_cursor = await database.execute(
            "SELECT 1 FROM season_archive_mounts WHERE guild_id = ?",
            (str(guild_id),),
        )
        if await mount_cursor.fetchone() is not None:
            raise RuntimeError("A season archive is already mounted in this server.")

        season = dict(prepared.rows_by_table["community_seasons"][0])
        season_number = int(season["season_number"])
        existing_cursor = await database.execute(
            """
            SELECT 1 FROM community_seasons
            WHERE guild_id = ? AND season_number = ?
            LIMIT 1
            """,
            (str(guild_id), season_number),
        )
        if await existing_cursor.fetchone() is not None:
            raise RuntimeError(
                "That season is already present in the live database and does not need mounting."
            )

        season_name = str(
            season.get("display_name")
            or f"Community Season {season_number}"
        )
        cursor = await database.execute(
            """
            INSERT INTO community_seasons (
                guild_id, season_number, required_prefix, display_name,
                tracked_player_count, started_at, ended_at, is_active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                str(guild_id),
                season_number,
                season["required_prefix"],
                season_name,
                season["tracked_player_count"],
                season["started_at"],
                season.get("ended_at") or prepared.manifest.get("exported_at"),
            ),
        )
        mounted_season_id = int(cursor.lastrowid)

        for table_name in (
            "season_players",
            "season_voided_players",
            "season_matches",
        ):
            await _insert_archive_rows(
                database,
                table_name,
                prepared.rows_by_table[table_name],
                community_season_id=mounted_season_id,
            )
        await _insert_archive_rows(
            database,
            "account_links",
            prepared.rows_by_table["account_links"],
        )
        await database.execute(
            """
            INSERT INTO season_archive_mounts (
                guild_id, community_season_id, season_number,
                archive_sha256, mounted_at, mounted_by_discord_user_id
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(guild_id),
                mounted_season_id,
                season_number,
                prepared.archive_sha256,
                _utc_now(),
                str(mounted_by_discord_user_id),
            ),
        )
        await database.commit()
        return MountResult(
            season_name=season_name,
            season_number=season_number,
            row_counts={
                table_name: len(prepared.rows_by_table[table_name])
                for table_name in ARCHIVE_TABLES
            },
        )
    except Exception:
        if database.in_transaction:
            await database.rollback()
        raise
    finally:
        await database.close()


async def get_mounted_season(guild_id: int) -> dict | None:
    database = await connect()
    try:
        cursor = await database.execute(
            """
            SELECT season.*, mount.archive_sha256, mount.mounted_at
            FROM season_archive_mounts mount
            JOIN community_seasons season ON season.id = mount.community_season_id
            WHERE mount.guild_id = ?
            LIMIT 1
            """,
            (str(guild_id),),
        )
        row = await cursor.fetchone()
        return _row_dict(row) if row is not None else None
    finally:
        await database.close()


async def unmount_season_archive(guild_id: int) -> UnmountResult:
    database = await connect()
    compacted = False
    try:
        await database.execute("PRAGMA busy_timeout = 30000")
        await database.execute("BEGIN IMMEDIATE")
        cursor = await database.execute(
            """
            SELECT season.*
            FROM season_archive_mounts mount
            JOIN community_seasons season ON season.id = mount.community_season_id
            WHERE mount.guild_id = ?
            LIMIT 1
            """,
            (str(guild_id),),
        )
        season = await cursor.fetchone()
        if season is None:
            raise RuntimeError("No season archive is currently mounted.")

        season_id = int(season["id"])
        season_number = int(season["season_number"])
        row_counts: dict[str, int] = {"community_seasons": 1}
        for table_name in (
            "season_players",
            "season_voided_players",
            "season_matches",
        ):
            count_cursor = await database.execute(
                f"SELECT COUNT(*) AS count FROM {table_name} WHERE community_season_id = ?",
                (season_id,),
            )
            row_counts[table_name] = int((await count_cursor.fetchone())["count"])
        link_cursor = await database.execute(
            """
            SELECT COUNT(*) AS count FROM account_links
            WHERE guild_id = ? AND season_number = ?
            """,
            (str(guild_id), season_number),
        )
        row_counts["account_links"] = int((await link_cursor.fetchone())["count"])

        await database.execute(
            "DELETE FROM account_links WHERE guild_id = ? AND season_number = ?",
            (str(guild_id), season_number),
        )
        await database.execute(
            "DELETE FROM community_seasons WHERE id = ?",
            (season_id,),
        )
        await database.commit()
        try:
            await database.execute("VACUUM")
            compacted = True
        except Exception:
            compacted = False

        return UnmountResult(
            season_name=str(
                season["display_name"]
                or f"Community Season {season_number}"
            ),
            season_number=season_number,
            row_counts=row_counts,
            compacted=compacted,
        )
    except Exception:
        if database.in_transaction:
            await database.rollback()
        raise
    finally:
        await database.close()


async def unload_current_season(guild_id: int) -> UnloadResult:
    database = await connect()
    compacted = False
    try:
        await database.execute("PRAGMA busy_timeout = 30000")
        await database.execute("BEGIN IMMEDIATE")
        season, rows_by_table = await _season_snapshot(database, guild_id)
        season_number = int(season["season_number"])

        cursor = await database.execute(
            """
            SELECT * FROM season_archive_exports
            WHERE guild_id = ? AND season_number = ?
              AND delivered_at IS NOT NULL AND unloaded_at IS NULL
            LIMIT 1
            """,
            (str(guild_id), season_number),
        )
        exported = await cursor.fetchone()
        if exported is None:
            raise RuntimeError(
                "Export the current season successfully before unloading it."
            )

        current_fingerprint = _snapshot_sha256(rows_by_table)
        if current_fingerprint != str(exported["snapshot_sha256"]):
            raise RuntimeError(
                "The current season changed after its last export. "
                "Run `/season export` again so no recent data is lost."
            )

        row_counts = {
            table_name: len(rows_by_table[table_name])
            for table_name in ARCHIVE_TABLES
        }
        season_name = str(
            season.get("display_name")
            or f"Community Season {season_number}"
        )
        now = _utc_now()

        await database.execute(
            "DELETE FROM account_links WHERE guild_id = ? AND season_number = ?",
            (str(guild_id), season_number),
        )
        await database.execute(
            "DELETE FROM community_seasons WHERE id = ? AND guild_id = ?",
            (int(season["id"]), str(guild_id)),
        )
        await database.execute(
            "UPDATE daily_leaderboard_configs SET enabled = 0, updated_at = ? WHERE guild_id = ?",
            (now, str(guild_id)),
        )
        await database.execute(
            "UPDATE season_match_configs SET enabled = 0, updated_at = ? WHERE guild_id = ?",
            (now, str(guild_id)),
        )
        await database.execute(
            """
            UPDATE season_archive_exports
            SET unloaded_at = ?
            WHERE guild_id = ? AND season_number = ?
            """,
            (now, str(guild_id), season_number),
        )
        await database.commit()

        try:
            await database.execute("VACUUM")
            compacted = True
        except Exception:
            compacted = False

        return UnloadResult(
            season_name=season_name,
            season_number=season_number,
            row_counts=row_counts,
            compacted=compacted,
        )
    except Exception:
        if database.in_transaction:
            await database.rollback()
        raise
    finally:
        await database.close()
