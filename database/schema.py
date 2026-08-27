SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS community_seasons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id TEXT NOT NULL,
    season_number INTEGER NOT NULL,
    required_prefix TEXT NOT NULL,
    tracked_player_count INTEGER NOT NULL DEFAULT 10,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    is_active INTEGER NOT NULL DEFAULT 1
        CHECK (is_active IN (0, 1))
);

CREATE TABLE IF NOT EXISTS bot_admins (
    guild_id TEXT NOT NULL,
    discord_user_id TEXT NOT NULL,
    added_at TEXT NOT NULL,
    added_by_discord_user_id TEXT,

    PRIMARY KEY (guild_id, discord_user_id)
);

CREATE TABLE IF NOT EXISTS season_players (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    community_season_id INTEGER NOT NULL,
    proavalon_username TEXT NOT NULL COLLATE NOCASE,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,

    FOREIGN KEY (community_season_id)
        REFERENCES community_seasons(id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS account_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    guild_id TEXT NOT NULL,
    discord_user_id TEXT NOT NULL,
    proavalon_username TEXT NOT NULL COLLATE NOCASE,
    season_number INTEGER,

    linked_at TEXT NOT NULL,
    unlinked_at TEXT,

    is_active INTEGER NOT NULL DEFAULT 1
        CHECK (is_active IN (0, 1))
);

CREATE UNIQUE INDEX IF NOT EXISTS one_season_number_per_guild
ON community_seasons (
    guild_id,
    season_number
);

CREATE UNIQUE INDEX IF NOT EXISTS one_required_prefix_per_guild
ON community_seasons (
    guild_id,
    required_prefix COLLATE NOCASE
);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_community_season_per_guild
ON community_seasons(guild_id)
WHERE is_active = 1;

CREATE UNIQUE INDEX IF NOT EXISTS one_player_per_season
ON season_players (
    community_season_id,
    proavalon_username COLLATE NOCASE
);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_discord_link_per_guild_season
ON account_links (
    guild_id,
    discord_user_id,
    COALESCE(season_number, -1)
)
WHERE is_active = 1;

CREATE UNIQUE INDEX IF NOT EXISTS one_active_proavalon_link_per_guild_season
ON account_links (
    guild_id,
    proavalon_username COLLATE NOCASE,
    COALESCE(season_number, -1)
)
WHERE is_active = 1;

CREATE INDEX IF NOT EXISTS account_links_by_guild_discord
ON account_links (
    guild_id,
    discord_user_id,
    season_number
);

CREATE INDEX IF NOT EXISTS account_links_by_guild_username
ON account_links (
    guild_id,
    proavalon_username COLLATE NOCASE,
    season_number
);

CREATE INDEX IF NOT EXISTS community_seasons_by_guild
ON community_seasons(guild_id);




CREATE TABLE IF NOT EXISTS season_voided_players (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    community_season_id INTEGER NOT NULL,
    proavalon_username TEXT NOT NULL COLLATE NOCASE,
    voided_by_discord_user_id TEXT NOT NULL,
    voided_at TEXT NOT NULL,

    FOREIGN KEY (community_season_id)
        REFERENCES community_seasons(id)
        ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS one_voided_player_per_season
ON season_voided_players (
    community_season_id,
    proavalon_username COLLATE NOCASE
);

CREATE TABLE IF NOT EXISTS custom_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id TEXT NOT NULL,
    name TEXT NOT NULL COLLATE NOCASE,
    tracked_player_count INTEGER NOT NULL DEFAULT 10,
    created_by_discord_user_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS one_custom_group_name_per_guild
ON custom_groups (
    guild_id,
    name COLLATE NOCASE
);

CREATE INDEX IF NOT EXISTS custom_groups_by_guild
ON custom_groups(guild_id);

CREATE TABLE IF NOT EXISTS custom_group_players (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    custom_group_id INTEGER NOT NULL,
    proavalon_username TEXT NOT NULL COLLATE NOCASE,
    added_by_discord_user_id TEXT NOT NULL,
    added_at TEXT NOT NULL,
    guild_id TEXT,
    discord_user_id TEXT,

    FOREIGN KEY (custom_group_id)
        REFERENCES custom_groups(id)
        ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS one_player_per_custom_group
ON custom_group_players (
    custom_group_id,
    proavalon_username COLLATE NOCASE
);


CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id TEXT PRIMARY KEY,
    timezone TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_leaderboard_configs (
    guild_id TEXT PRIMARY KEY,
    leaderboard_type TEXT NOT NULL
        CHECK (leaderboard_type IN ('total_time', 'games_played', 'rating', 'overall', 'resistance', 'spy', 'vt', 'lover', 'percival', 'merlin', 'assassin', 'morgana', 'mordred', 'oberon')),
    channel_id TEXT NOT NULL,
    post_time TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1
        CHECK (enabled IN (0, 1)),
    minimum_games INTEGER NOT NULL DEFAULT 5,
    top_count INTEGER NOT NULL DEFAULT 15,
    last_post_local_date TEXT,
    updated_at TEXT NOT NULL
);


CREATE TABLE IF NOT EXISTS proavalon_ratings (
    proavalon_username TEXT PRIMARY KEY COLLATE NOCASE,
    rating INTEGER NOT NULL,
    rating_bracket TEXT,
    last_seen_online_at TEXT NOT NULL,
    rating_updated_at TEXT NOT NULL
);


CREATE TABLE IF NOT EXISTS season_match_configs (
    guild_id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS season_matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    community_season_id INTEGER NOT NULL,
    season_match_id INTEGER,
    match_fingerprint TEXT NOT NULL,
    room_id INTEGER,
    detected_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    winner TEXT,
    game_mode TEXT,
    mission_history_json TEXT NOT NULL,
    fails_history_json TEXT NOT NULL,
    vote_history_json TEXT,
    players_json TEXT NOT NULL,
    roles_json TEXT,
    rating_changes_json TEXT,
    posted_at TEXT,
    FOREIGN KEY (community_season_id) REFERENCES community_seasons(id) ON DELETE CASCADE,
    UNIQUE (community_season_id, match_fingerprint)
);

CREATE INDEX IF NOT EXISTS season_matches_by_season
ON season_matches(community_season_id, finished_at);

CREATE UNIQUE INDEX IF NOT EXISTS one_match_number_per_season
ON season_matches(community_season_id, season_match_id);

CREATE INDEX IF NOT EXISTS proavalon_ratings_updated
ON proavalon_ratings(rating_updated_at);


CREATE TABLE IF NOT EXISTS command_channel_configs (
    guild_id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback_channel_configs (
    guild_id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS suggestion_recipients (
    guild_id TEXT NOT NULL,
    discord_user_id TEXT NOT NULL,
    added_at TEXT NOT NULL,

    PRIMARY KEY (guild_id, discord_user_id)
);

CREATE INDEX IF NOT EXISTS suggestion_recipients_by_guild
ON suggestion_recipients(guild_id);


CREATE TABLE IF NOT EXISTS daily_leaderboard_channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id TEXT NOT NULL,
    leaderboard_type TEXT NOT NULL
        CHECK (leaderboard_type IN ('total_time', 'games_played', 'rating', 'overall', 'resistance', 'spy', 'vt', 'lover', 'percival', 'merlin', 'assassin', 'morgana', 'mordred', 'oberon')),
    channel_id TEXT NOT NULL,
    post_time TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1
        CHECK (enabled IN (0, 1)),
    minimum_games INTEGER NOT NULL DEFAULT 5,
    top_count INTEGER NOT NULL DEFAULT 500,
    last_post_local_date TEXT,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS one_daily_leaderboard_per_guild_channel
ON daily_leaderboard_channels (guild_id, channel_id);

CREATE INDEX IF NOT EXISTS daily_leaderboard_channels_enabled
ON daily_leaderboard_channels (enabled, guild_id);



CREATE TABLE IF NOT EXISTS discord_role_configs (
    guild_id TEXT NOT NULL,
    role_key TEXT NOT NULL,
    role_id TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (guild_id, role_key)
);

CREATE UNIQUE INDEX IF NOT EXISTS one_discord_role_config_per_role
ON discord_role_configs (guild_id, role_id);

CREATE TABLE IF NOT EXISTS discord_role_settings (
    guild_id TEXT PRIMARY KEY,
    award_minimum_games INTEGER NOT NULL DEFAULT 5,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS discord_role_award_settings (
    guild_id TEXT NOT NULL,
    role_key TEXT NOT NULL,
    minimum_games INTEGER NOT NULL DEFAULT 20
        CHECK (minimum_games >= 1),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (guild_id, role_key)
);

CREATE TABLE IF NOT EXISTS discord_role_assignments (
    guild_id TEXT NOT NULL,
    assignment_key TEXT NOT NULL,
    discord_user_id TEXT NOT NULL,
    role_id TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (guild_id, assignment_key)
);

CREATE INDEX IF NOT EXISTS discord_role_assignments_by_user
ON discord_role_assignments (guild_id, discord_user_id);

"""