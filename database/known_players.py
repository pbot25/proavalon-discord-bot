from database.connection import connect


def _guild_id_text(guild_id: int) -> str:
    if guild_id <= 0:
        raise ValueError("guild_id must be a positive integer.")

    return str(guild_id)


async def get_known_proavalon_usernames(
    guild_id: int,
) -> list[str]:
    """
    Return every ProAvalon username this Discord server already knows.

    Sources:
    - current and archived community-season player rosters
    - active account links
    - custom-group player rosters

    Results are deduplicated case-insensitively and sorted alphabetically.
    """

    database = await connect()

    try:
        cursor = await database.execute(
            """
            SELECT proavalon_username
            FROM (
                SELECT
                    season_players.proavalon_username
                        AS proavalon_username
                FROM season_players

                INNER JOIN community_seasons
                    ON community_seasons.id =
                        season_players.community_season_id

                WHERE community_seasons.guild_id = ?

                UNION

                SELECT
                    account_links.proavalon_username
                        AS proavalon_username
                FROM account_links
                WHERE account_links.guild_id = ?
                  AND account_links.is_active = 1

                UNION

                SELECT
                    custom_group_players.proavalon_username
                        AS proavalon_username
                FROM custom_group_players

                INNER JOIN custom_groups
                    ON custom_groups.id =
                        custom_group_players.custom_group_id

                WHERE custom_groups.guild_id = ?
            )
            ORDER BY proavalon_username COLLATE NOCASE ASC
            """,
            (
                _guild_id_text(guild_id),
                _guild_id_text(guild_id),
                _guild_id_text(guild_id),
            ),
        )

        rows = await cursor.fetchall()

        # UNION already removes exact duplicates, but do one final
        # case-insensitive pass because SQLite's UNION comparison does
        # not necessarily collapse differently-cased spellings.
        seen: set[str] = set()
        usernames: list[str] = []

        for row in rows:
            username = str(
                row["proavalon_username"]
            ).strip()

            if not username:
                continue

            normalized = username.casefold()

            if normalized in seen:
                continue

            seen.add(normalized)
            usernames.append(username)

        return usernames

    finally:
        await database.close()
