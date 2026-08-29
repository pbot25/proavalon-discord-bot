from datetime import datetime, timezone

from database.connection import connect


class DiscordAccountAlreadyLinkedError(Exception):
    pass


class ProAvalonAccountAlreadyLinkedError(Exception):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(value: datetime) -> str:
    return value.isoformat()


def _guild_id_text(guild_id: int) -> str:
    if guild_id <= 0:
        raise ValueError("guild_id must be a positive integer.")
    return str(guild_id)


def _season_clause(season_number: int | None) -> tuple[str, tuple]:
    if season_number is None:
        return "season_number IS NULL", ()
    return "season_number = ?", (int(season_number),)




async def _get_active_link_for_discord_scope(
    guild_id: int,
    discord_user_id: int,
    season_number: int | None,
):
    database = await connect()
    try:
        if season_number is None:
            season_sql = "season_number IS NULL"
            params = (_guild_id_text(guild_id), str(discord_user_id))
        else:
            season_sql = "season_number = ?"
            params = (
                _guild_id_text(guild_id),
                str(discord_user_id),
                int(season_number),
            )
        cursor = await database.execute(
            f"""
            SELECT id,guild_id,discord_user_id,proavalon_username,
                   season_number,linked_at,unlinked_at,is_active
            FROM account_links
            WHERE guild_id = ? AND discord_user_id = ?
              AND is_active = 1 AND {season_sql}
            ORDER BY linked_at DESC, id DESC
            LIMIT 1
            """,
            params,
        )
        return await cursor.fetchone()
    finally:
        await database.close()


async def _get_active_link_for_username_scope(
    guild_id: int,
    proavalon_username: str,
    season_number: int | None,
):
    database = await connect()
    try:
        if season_number is None:
            season_sql = "season_number IS NULL"
            params = (_guild_id_text(guild_id), proavalon_username.strip())
        else:
            season_sql = "season_number = ?"
            params = (
                _guild_id_text(guild_id),
                proavalon_username.strip(),
                int(season_number),
            )
        cursor = await database.execute(
            f"""
            SELECT id,guild_id,discord_user_id,proavalon_username,
                   season_number,linked_at,unlinked_at,is_active
            FROM account_links
            WHERE guild_id = ?
              AND proavalon_username = ? COLLATE NOCASE
              AND is_active = 1 AND {season_sql}
            ORDER BY linked_at DESC, id DESC
            LIMIT 1
            """,
            params,
        )
        return await cursor.fetchone()
    finally:
        await database.close()




async def repair_account_link_scopes_for_guild(
    guild_id: int,
) -> int:
    """
    Repair active account links to the configured season prefix in this guild.
    Returns the number of rows changed or retired as duplicate legacy links.
    """
    guild_text = _guild_id_text(guild_id)
    database = await connect()
    changed = 0

    try:
        season_cursor = await database.execute(
            """
            SELECT id, season_number, required_prefix
            FROM community_seasons
            WHERE guild_id = ?
            ORDER BY LENGTH(required_prefix) DESC, id DESC
            """,
            (guild_text,),
        )
        seasons = await season_cursor.fetchall()
        if not seasons:
            return 0

        link_cursor = await database.execute(
            """
            SELECT *
            FROM account_links
            WHERE guild_id = ?
              AND is_active = 1
            ORDER BY id ASC
            """,
            (guild_text,),
        )
        links = await link_cursor.fetchall()

        await database.execute("BEGIN")

        for link in links:
            username = str(link["proavalon_username"] or "").strip()
            lowered = username.casefold()
            target = next(
                (
                    season
                    for season in seasons
                    if str(season["required_prefix"] or "").strip()
                    and lowered.startswith(
                        str(season["required_prefix"]).strip().casefold()
                    )
                ),
                None,
            )
            if target is None:
                continue

            target_number = int(target["season_number"])
            current_number = link["season_number"]
            if current_number is not None and int(current_number) == target_number:
                continue

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
                    guild_text,
                    int(link["id"]),
                    target_number,
                    str(link["discord_user_id"]),
                    username,
                ),
            )
            conflict = await conflict_cursor.fetchone()

            if conflict is not None:
                same_pair = (
                    str(conflict["discord_user_id"])
                    == str(link["discord_user_id"])
                    and str(conflict["proavalon_username"]).casefold()
                    == username.casefold()
                )
                if same_pair:
                    await database.execute(
                        """
                        UPDATE account_links
                        SET is_active = 0,
                            unlinked_at = COALESCE(unlinked_at, linked_at)
                        WHERE id = ?
                        """,
                        (int(link["id"]),),
                    )
                    changed += 1
                continue

            await database.execute(
                """
                UPDATE account_links
                SET season_number = ?
                WHERE id = ?
                """,
                (target_number, int(link["id"])),
            )
            changed += 1

        await database.commit()
        return changed

    except Exception:
        await database.rollback()
        raise
    finally:
        await database.close()

async def get_active_link_for_discord(
    guild_id: int,
    discord_user_id: int,
    season_number: int | None = None,
):
    await repair_account_link_scopes_for_guild(guild_id)
    """
    Return a saved active link.

    If season_number is supplied, the lookup is scoped to that season.
    If omitted, the most recently linked active account is returned.
    """
    database = await connect()
    try:
        params: list[object] = [
            _guild_id_text(guild_id),
            str(discord_user_id),
        ]
        season_sql = ""
        if season_number is not None:
            season_sql = " AND season_number = ?"
            params.append(int(season_number))

        cursor = await database.execute(
            f"""
            SELECT
                id,
                guild_id,
                discord_user_id,
                proavalon_username,
                season_number,
                linked_at,
                unlinked_at,
                is_active
            FROM account_links
            WHERE guild_id = ?
              AND discord_user_id = ?
              AND is_active = 1
              {season_sql}
            ORDER BY linked_at DESC, id DESC
            LIMIT 1
            """,
            tuple(params),
        )
        return await cursor.fetchone()
    finally:
        await database.close()


async def get_preferred_link_for_discord(
    guild_id: int,
    discord_user_id: int,
    season_number: int | None,
):
    await repair_account_link_scopes_for_guild(guild_id)
    """Prefer the active season's saved link, then fall back to latest."""
    if season_number is not None:
        seasonal = await get_active_link_for_discord(
            guild_id,
            discord_user_id,
            season_number,
        )
        if seasonal is not None:
            return seasonal

    return await get_active_link_for_discord(
        guild_id,
        discord_user_id,
    )


async def get_active_link_for_username(
    guild_id: int,
    proavalon_username: str,
    season_number: int | None = None,
):
    database = await connect()
    try:
        params: list[object] = [
            _guild_id_text(guild_id),
            proavalon_username.strip(),
        ]
        season_sql = ""
        if season_number is not None:
            season_sql = " AND season_number = ?"
            params.append(int(season_number))

        cursor = await database.execute(
            f"""
            SELECT
                id,
                guild_id,
                discord_user_id,
                proavalon_username,
                season_number,
                linked_at,
                unlinked_at,
                is_active
            FROM account_links
            WHERE guild_id = ?
              AND proavalon_username = ? COLLATE NOCASE
              AND is_active = 1
              {season_sql}
            ORDER BY linked_at DESC, id DESC
            LIMIT 1
            """,
            tuple(params),
        )
        return await cursor.fetchone()
    finally:
        await database.close()


async def get_saved_links_for_discord(
    guild_id: int,
    discord_user_id: int,
):
    await repair_account_link_scopes_for_guild(guild_id)
    database = await connect()
    try:
        cursor = await database.execute(
            """
            SELECT
                id,
                guild_id,
                discord_user_id,
                proavalon_username,
                season_number,
                linked_at,
                unlinked_at,
                is_active
            FROM account_links
            WHERE guild_id = ?
              AND discord_user_id = ?
              AND is_active = 1
            ORDER BY
                CASE WHEN season_number IS NULL THEN 1 ELSE 0 END,
                season_number ASC,
                linked_at DESC
            """,
            (
                _guild_id_text(guild_id),
                str(discord_user_id),
            ),
        )
        return await cursor.fetchall()
    finally:
        await database.close()


async def create_account_link(
    guild_id: int,
    discord_user_id: int,
    proavalon_username: str,
    season_number: int | None = None,
) -> None:
    await repair_account_link_scopes_for_guild(guild_id)
    """
    Save a link for a specific season without removing links from other seasons.

    Re-linking the same Discord user inside the same season replaces only that
    season's current link while preserving archived links for other seasons.
    """
    canonical_username = proavalon_username.strip()
    if not canonical_username:
        raise ValueError("ProAvalon username cannot be empty.")

    existing_same_username = await _get_active_link_for_username_scope(
        guild_id,
        canonical_username,
        season_number,
    )
    if (
        existing_same_username is not None
        and str(existing_same_username["discord_user_id"])
        != str(discord_user_id)
    ):
        label = (
            f"Season {season_number}"
            if season_number is not None
            else "the general link slot"
        )
        raise ProAvalonAccountAlreadyLinkedError(
            f"That ProAvalon account is already linked to another "
            f"Discord user for {label} in this server."
        )

    existing_discord_link = await _get_active_link_for_discord_scope(
        guild_id,
        discord_user_id,
        season_number,
    )
    if existing_discord_link is not None:
        if (
            str(existing_discord_link["proavalon_username"]).casefold()
            == canonical_username.casefold()
        ):
            raise DiscordAccountAlreadyLinkedError(
                "That Discord user is already linked to this ProAvalon "
                "account for the same season."
            )

    database = await connect()
    try:
        await database.execute("BEGIN")

        # Replace only the link for this same Discord user + same season.
        if season_number is None:
            await database.execute(
                """
                UPDATE account_links
                SET is_active = 0, unlinked_at = ?
                WHERE guild_id = ?
                  AND discord_user_id = ?
                  AND season_number IS NULL
                  AND is_active = 1
                """,
                (
                    to_iso(utc_now()),
                    _guild_id_text(guild_id),
                    str(discord_user_id),
                ),
            )
        else:
            await database.execute(
                """
                UPDATE account_links
                SET is_active = 0, unlinked_at = ?
                WHERE guild_id = ?
                  AND discord_user_id = ?
                  AND season_number = ?
                  AND is_active = 1
                """,
                (
                    to_iso(utc_now()),
                    _guild_id_text(guild_id),
                    str(discord_user_id),
                    int(season_number),
                ),
            )

        await database.execute(
            """
            INSERT INTO account_links (
                guild_id,
                discord_user_id,
                proavalon_username,
                season_number,
                linked_at,
                unlinked_at,
                is_active
            )
            VALUES (?, ?, ?, ?, ?, NULL, 1)
            """,
            (
                _guild_id_text(guild_id),
                str(discord_user_id),
                canonical_username,
                int(season_number) if season_number is not None else None,
                to_iso(utc_now()),
            ),
        )
        await database.commit()
    except Exception:
        await database.rollback()
        raise
    finally:
        await database.close()


async def unlink_discord_account(
    guild_id: int,
    discord_user_id: int,
    season_number: int | None = None,
):
    await repair_account_link_scopes_for_guild(guild_id)
    link = await _get_active_link_for_discord_scope(
        guild_id,
        discord_user_id,
        season_number,
    )
    if link is None:
        return None

    database = await connect()
    try:
        await database.execute(
            """
            UPDATE account_links
            SET is_active = 0, unlinked_at = ?
            WHERE id = ? AND guild_id = ?
            """,
            (
                to_iso(utc_now()),
                link["id"],
                _guild_id_text(guild_id),
            ),
        )
        await database.commit()
        return link
    finally:
        await database.close()


async def force_unlink_username(
    guild_id: int,
    proavalon_username: str,
    season_number: int | None = None,
):
    link = await get_active_link_for_username(
        guild_id,
        proavalon_username,
        season_number,
    )
    if link is None:
        return None

    database = await connect()
    try:
        await database.execute(
            """
            UPDATE account_links
            SET is_active = 0, unlinked_at = ?
            WHERE id = ? AND guild_id = ?
            """,
            (
                to_iso(utc_now()),
                link["id"],
                _guild_id_text(guild_id),
            ),
        )
        await database.commit()
        return link
    finally:
        await database.close()


async def get_registered_players_with_links(
    guild_id: int,
    community_season_id: int,
):
    await repair_account_link_scopes_for_guild(guild_id)
    database = await connect()
    try:
        cursor = await database.execute(
            """
            SELECT
                season_players.proavalon_username,
                season_players.first_seen_at,
                season_players.last_seen_at,
                account_links.discord_user_id
            FROM season_players

            INNER JOIN community_seasons
                ON community_seasons.id = season_players.community_season_id

            LEFT JOIN account_links
                ON account_links.guild_id = ?
               AND account_links.proavalon_username =
                    season_players.proavalon_username COLLATE NOCASE
               AND account_links.season_number = community_seasons.season_number
               AND account_links.is_active = 1

            WHERE season_players.community_season_id = ?

            ORDER BY season_players.proavalon_username COLLATE NOCASE ASC
            """,
            (
                _guild_id_text(guild_id),
                community_season_id,
            ),
        )
        return await cursor.fetchall()
    finally:
        await database.close()


async def get_unlinked_registered_usernames(
    guild_id: int,
    community_season_id: int,
) -> list[str]:
    database = await connect()
    try:
        cursor = await database.execute(
            """
            SELECT season_players.proavalon_username
            FROM season_players

            INNER JOIN community_seasons
                ON community_seasons.id = season_players.community_season_id

            LEFT JOIN account_links
                ON account_links.guild_id = ?
               AND account_links.proavalon_username =
                    season_players.proavalon_username COLLATE NOCASE
               AND account_links.season_number = community_seasons.season_number
               AND account_links.is_active = 1

            WHERE season_players.community_season_id = ?
              AND account_links.id IS NULL

            ORDER BY season_players.proavalon_username COLLATE NOCASE ASC
            """,
            (
                _guild_id_text(guild_id),
                community_season_id,
            ),
        )
        rows = await cursor.fetchall()
        return [row["proavalon_username"] for row in rows]
    finally:
        await database.close()


async def get_linked_usernames(
    guild_id: int,
) -> list[str]:
    database = await connect()
    try:
        cursor = await database.execute(
            """
            SELECT DISTINCT proavalon_username
            FROM account_links
            WHERE guild_id = ?
              AND is_active = 1
            ORDER BY proavalon_username COLLATE NOCASE ASC
            """,
            (_guild_id_text(guild_id),),
        )
        rows = await cursor.fetchall()
        return [row["proavalon_username"] for row in rows]
    finally:
        await database.close()
