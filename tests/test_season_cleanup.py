from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

import database.connection as connection
from database.connection import initialize_database
from database.seasons import (
    cleanup_ended_season_operational_data,
    end_active_season,
)
from database.insights import search_guild_matches_by_players
from database.account_links import get_registered_players_with_links
from commands.stats import player_username_autocomplete


class SeasonCleanupTests(unittest.IsolatedAsyncioTestCase):
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
                ) VALUES ('123', 4, 's4', 'Season 4', 10,
                          '2000-01-01T00:00:00+00:00', 1)
                """
            )
            await database.execute(
                """
                INSERT INTO season_players (
                    community_season_id, proavalon_username,
                    first_seen_at, last_seen_at
                ) VALUES (1, 's4player', '2000-01-01', '2001-01-01')
                """
            )
            await database.execute(
                """
                INSERT INTO account_links (
                    guild_id, discord_user_id, proavalon_username,
                    season_number, linked_at, is_active
                ) VALUES ('123', '456', 's4player', 4, '2000-01-01', 1)
                """
            )
            await database.execute(
                """
                INSERT INTO account_links (
                    guild_id, discord_user_id, proavalon_username,
                    season_number, linked_at, is_active
                ) VALUES ('123', '789', 's4zerogames', 4, '2000-01-02', 1)
                """
            )
            await database.execute(
                """
                INSERT INTO season_matches (
                    community_season_id, season_match_id, match_fingerprint,
                    room_id, detected_at, finished_at, mission_history_json,
                    fails_history_json, vote_history_json, players_json,
                    roles_json, rating_changes_json, posted_message_id,
                    youtube_url, recording_status
                ) VALUES (1, 1, 'match-1', 10, '2001-01-01', '2001-01-01',
                          '["succeeded"]', '[0]', '{"votes": [1, 2, 3]}',
                          '["s4player"]', '["merlin"]', '{"delta": 5}', '999',
                          'https://youtu.be/example', 'ready')
                """
            )
            await database.execute(
                """
                INSERT INTO season_voided_players (
                    community_season_id, proavalon_username,
                    voided_by_discord_user_id, voided_at
                ) VALUES (1, 's4voided', '456', '2001-01-01')
                """
            )
            await database.execute(
                """
                INSERT INTO season_match_configs (
                    guild_id, channel_id, enabled, updated_at
                ) VALUES ('123', '777', 1, '2001-01-01')
                """
            )
            await database.execute(
                """
                INSERT INTO daily_leaderboard_channels (
                    guild_id, leaderboard_type, channel_id, post_time,
                    enabled, minimum_games, top_count, updated_at
                ) VALUES ('123', 'rating', '888', '20:00', 1, 5, 500,
                          '2001-01-01')
                """
            )
            await database.executemany(
                """
                INSERT INTO recording_audit_events (
                    guild_id, room_id, stage, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                [
                    ('123', 10, 'season-event', '2001-01-01T00:00:00+00:00'),
                    ('123', 11, 'older-event', '1999-01-01T00:00:00+00:00'),
                    ('999', 12, 'other-guild', '2001-01-01T00:00:00+00:00'),
                ],
            )
            await database.commit()
        finally:
            await database.close()

    async def asyncTearDown(self) -> None:
        connection.DATABASE_PATH = self.original_path
        self.temporary.cleanup()

    async def test_cleanup_preserves_historical_season_data(self) -> None:
        season = await end_active_season(123)
        choices = await player_username_autocomplete(
            SimpleNamespace(guild_id=123),
            "s4",
        )
        self.assertEqual(choices, [])
        result = await cleanup_ended_season_operational_data(
            123,
            int(season["id"]),
            season["started_at"],
        )
        self.assertEqual(result["deleted_audit_events"], 1)
        self.assertEqual(result["deleted_voided_players"], 1)
        self.assertEqual(result["compacted_matches"], 1)

        database = await connection.connect()
        try:
            async def count(table_name: str) -> int:
                cursor = await database.execute(
                    f"SELECT COUNT(*) AS count FROM {table_name}"
                )
                return int((await cursor.fetchone())["count"])

            self.assertEqual(await count("community_seasons"), 1)
            self.assertEqual(await count("season_players"), 1)
            self.assertEqual(await count("account_links"), 2)
            self.assertEqual(await count("season_matches"), 1)
            self.assertEqual(await count("season_voided_players"), 0)
            self.assertEqual(await count("recording_audit_events"), 2)

            match_config = await (
                await database.execute(
                    "SELECT enabled, channel_id FROM season_match_configs "
                    "WHERE guild_id = '123'"
                )
            ).fetchone()
            self.assertEqual(match_config["enabled"], 1)
            self.assertEqual(match_config["channel_id"], "777")

            daily_config = await (
                await database.execute(
                    "SELECT enabled, channel_id, post_time "
                    "FROM daily_leaderboard_channels WHERE guild_id = '123'"
                )
            ).fetchone()
            self.assertEqual(daily_config["enabled"], 1)
            self.assertEqual(daily_config["channel_id"], "888")
            self.assertEqual(daily_config["post_time"], "20:00")

            match = await (
                await database.execute(
                    """SELECT youtube_url, players_json, roles_json,
                              mission_history_json, vote_history_json,
                              rating_changes_json, posted_message_id,
                              recording_status
                       FROM season_matches LIMIT 1"""
                )
            ).fetchone()
            self.assertEqual(match["youtube_url"], "https://youtu.be/example")
            self.assertEqual(match["players_json"], '["s4player"]')
            self.assertEqual(match["roles_json"], '["merlin"]')
            self.assertEqual(match["mission_history_json"], '["succeeded"]')
            self.assertIsNone(match["vote_history_json"])
            self.assertIsNone(match["rating_changes_json"])
            self.assertIsNone(match["posted_message_id"])
            self.assertIsNone(match["recording_status"])
        finally:
            await database.close()

    async def test_registered_players_include_linked_zero_game_accounts(self) -> None:
        players = await get_registered_players_with_links(123, 1)
        by_name = {
            str(player["proavalon_username"]): player
            for player in players
        }
        self.assertEqual(set(by_name), {"s4player", "s4zerogames"})
        self.assertEqual(by_name["s4zerogames"]["discord_user_id"], "789")

    async def test_match_search_spans_seasons_and_labels_results(self) -> None:
        database = await connection.connect()
        try:
            await database.execute(
                """
                INSERT INTO community_seasons (
                    guild_id, season_number, required_prefix, display_name,
                    tracked_player_count, started_at, is_active
                ) VALUES ('123', 5, 's5', 'Ranked Res - Season 5', 10,
                          '2002-01-01T00:00:00+00:00', 0)
                """
            )
            await database.execute(
                """
                INSERT INTO season_matches (
                    community_season_id, season_match_id, match_fingerprint,
                    detected_at, finished_at, mission_history_json,
                    fails_history_json, players_json, roles_json
                ) VALUES (2, 1, 'match-2', '2002-01-01', '2002-01-01',
                          '[]', '[]', '["s4player", "s5friend"]',
                          '["merlin", "assassin"]')
                """
            )
            await database.commit()
        finally:
            await database.close()

        rows = await search_guild_matches_by_players(123, ["s4player"])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["season_display_name"], "Ranked Res - Season 5")
        self.assertEqual(rows[1]["season_display_name"], "Season 4")


if __name__ == "__main__":
    unittest.main()
