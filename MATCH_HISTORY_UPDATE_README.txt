MATCH HISTORY STARTER UPDATE

Replace your current bot folder with the files in this ZIP (keep your own .env).

New behavior
- One authenticated ProAvalon socket now handles both live ratings and match tracking.
- Every active Discord community season is monitored by its S<number> prefix.
- A full 10-player / 10-seat ProAvalon room qualifies when all 10 usernames match the active season prefix (case-insensitive).
- Players discovered in a qualifying match are automatically registered in that server's season.
- Explicitly voided players stay voided; automatic discovery never unvoids them.
- Match detection does not depend on any players having been registered beforehand.
- Qualifying matches are persisted in SQLite even if no match-post channel is configured.
- Finished matches store winner, players, roles, missions, fail history, vote history, and observed rating changes.
- Duplicate completed matches are protected by a deterministic fingerprint.
- The Discord result includes a generated voting-history PNG when ProAvalon vote history is enabled.
- Rating changes and rank changes use the bot's before/after live-rating observations. If a rating was never observed, it displays N/A rather than inventing a value.

New season commands
/season match-channel #channel
/season match-channel-info
/season match-channel-disable

The set/disable commands use the existing server-specific bot-admin permission system.

Account linking
/account link now validates only that the ProAvalon profile/account exists. It no longer requires profile statistics to have been found, so a brand-new zero-game account can link before a season starts.

Concurrency
The single ProAvalon socket rotates through simultaneous full rooms. ProAvalon's spectator game-data is cumulative, so revisiting a room receives its accumulated vote/mission state rather than only events seen while physically in that room.

Dependency
Pillow==11.3.0 was added to requirements.txt for voting-history PNG generation.
Run:
    pip install -r requirements.txt

Database
No manual SQL is needed. Startup creates season_match_configs and season_matches automatically.
A migration is also included so older daily_leaderboard_configs tables accept overall/resistance/spy/rating.

Important operational note
The tracker cannot recover matches that finished entirely while the Discord bot was offline. It begins observing matches while the bot is running.
