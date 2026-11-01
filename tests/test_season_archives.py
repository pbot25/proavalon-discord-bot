import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import zipfile

import database.connection as connection
from database.connection import initialize_database
from database.season_archives import (
    build_current_season_archive,
    get_mounted_season,
    mount_prepared_season_archive,
    prepare_season_archive_mount,
    record_delivered_export,
    unmount_season_archive,
    unload_current_season,
)
from database.seasons import create_season, get_active_season, get_viewable_season


class SeasonArchiveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.original_path = connection.DATABASE_PATH
        connection.DATABASE_PATH = Path(self.temporary.name) / "proavalon.db"
        await initialize_database()

        database = await connection.connect()
        try:
            await database.execute(
                """
                INSERT INTO community_seasons (
                    guild_id, season_number, required_prefix, display_name,
                    tracked_player_count, started_at, is_active
                ) VALUES ('123', 4, 's4', 'Ranked Res - Season 4', 10, 'now', 1)
                """
            )
            await database.execute(
                """
                INSERT INTO season_players (
                    community_season_id, proavalon_username,
                    first_seen_at, last_seen_at
                ) VALUES (1, 's4player', 'now', 'now')
                """
            )
            await database.execute(
                """
                INSERT INTO account_links (
                    guild_id, discord_user_id, proavalon_username,
                    season_number, linked_at, is_active
                ) VALUES ('123', '456', 's4player', 4, 'now', 1)
                """
            )
            await database.execute(
                """
                INSERT INTO season_matches (
                    community_season_id, season_match_id, match_fingerprint,
                    detected_at, finished_at, mission_history_json,
                    fails_history_json, players_json
                ) VALUES (1, 1, 'match-1', 'now', 'now', '[]', '[]', '["s4player"]')
                """
            )
            await database.commit()
        finally:
            await database.close()

    async def asyncTearDown(self) -> None:
        connection.DATABASE_PATH = self.original_path
        self.temporary.cleanup()

    async def test_export_contains_season_data(self) -> None:
        archive = await build_current_season_archive(123)
        self.addCleanup(archive.path.unlink, missing_ok=True)

        self.assertEqual(archive.row_counts["season_matches"], 1)
        self.assertEqual(archive.row_counts["season_players"], 1)
        with zipfile.ZipFile(archive.path) as bundle:
            manifest = json.loads(bundle.read("manifest.json"))
            self.assertEqual(manifest["season_number"], 4)
            extracted = Path(self.temporary.name) / "archive.sqlite3"
            extracted.write_bytes(bundle.read("season.sqlite3"))

        archived_database = sqlite3.connect(extracted)
        try:
            count = archived_database.execute(
                "SELECT COUNT(*) FROM season_matches"
            ).fetchone()[0]
            self.assertEqual(count, 1)
        finally:
            archived_database.close()

    async def test_export_can_target_latest_ended_season(self) -> None:
        database = await connection.connect()
        try:
            await database.execute(
                "UPDATE community_seasons SET is_active = 0, ended_at = 'later'"
            )
            await database.commit()
        finally:
            await database.close()

        archive = await build_current_season_archive(123)
        self.addCleanup(archive.path.unlink, missing_ok=True)
        self.assertEqual(archive.season_number, 4)
        await record_delivered_export(archive, 456)
        result = await unload_current_season(123)
        self.assertEqual(result.season_number, 4)

    async def test_unload_rejects_changed_season_then_accepts_new_export(self) -> None:
        first = await build_current_season_archive(123)
        self.addCleanup(first.path.unlink, missing_ok=True)
        await record_delivered_export(first, 456)

        database = await connection.connect()
        try:
            await database.execute(
                """
                INSERT INTO season_matches (
                    community_season_id, season_match_id, match_fingerprint,
                    detected_at, finished_at, mission_history_json,
                    fails_history_json, players_json
                ) VALUES (1, 2, 'match-2', 'later', 'later', '[]', '[]', '["s4player"]')
                """
            )
            await database.commit()
        finally:
            await database.close()

        with self.assertRaisesRegex(RuntimeError, "changed after its last export"):
            await unload_current_season(123)

        second = await build_current_season_archive(123)
        self.addCleanup(second.path.unlink, missing_ok=True)
        await record_delivered_export(second, 456)
        result = await unload_current_season(123)
        self.assertEqual(result.row_counts["season_matches"], 2)

        database = await connection.connect()
        try:
            season_count = await (
                await database.execute("SELECT COUNT(*) AS count FROM community_seasons")
            ).fetchone()
            link_count = await (
                await database.execute("SELECT COUNT(*) AS count FROM account_links")
            ).fetchone()
            catalog = await (
                await database.execute(
                    "SELECT unloaded_at FROM season_archive_exports WHERE guild_id = '123'"
                )
            ).fetchone()
            self.assertEqual(season_count["count"], 0)
            self.assertEqual(link_count["count"], 0)
            self.assertIsNotNone(catalog["unloaded_at"])
        finally:
            await database.close()

        next_season = await create_season(123, "s5", "Ranked Res - Season 5")
        self.assertEqual(next_season["season_number"], 5)

    async def test_unloaded_archive_can_mount_read_only_and_unmount(self) -> None:
        archive = await build_current_season_archive(123)
        archive_bytes = archive.path.read_bytes()
        self.addCleanup(archive.path.unlink, missing_ok=True)
        await record_delivered_export(archive, 456)
        await unload_current_season(123)

        prepared = await prepare_season_archive_mount(
            123,
            archive_bytes,
            archive.filename,
        )
        mounted = await mount_prepared_season_archive(123, prepared, 456)
        self.assertEqual(mounted.row_counts["season_matches"], 1)
        self.assertIsNone(await get_active_season(123))
        viewable = await get_viewable_season(123)
        self.assertEqual(viewable["season_number"], 4)
        self.assertEqual(viewable["is_mounted"], 1)
        self.assertIsNotNone(await get_mounted_season(123))

        with self.assertRaisesRegex(RuntimeError, "Unmount"):
            await create_season(123, "s5", "Ranked Res - Season 5")

        unmounted = await unmount_season_archive(123)
        self.assertEqual(unmounted.row_counts["season_matches"], 1)
        self.assertIsNone(await get_viewable_season(123))
        self.assertIsNone(await get_mounted_season(123))

        next_season = await create_season(123, "s5", "Ranked Res - Season 5")
        self.assertEqual(next_season["season_number"], 5)


if __name__ == "__main__":
    unittest.main()
