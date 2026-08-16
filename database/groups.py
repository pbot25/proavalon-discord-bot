from datetime import datetime, timezone

from database.connection import connect


class CustomGroupAlreadyExistsError(Exception):
    pass


class CustomGroupPlayerAlreadyExistsError(Exception):
    pass


class CustomGroupMembershipError(Exception):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _guild_id_text(guild_id: int) -> str:
    if guild_id <= 0:
        raise ValueError("guild_id must be a positive integer.")
    return str(guild_id)


async def _ensure_group_membership_schema(database) -> None:
    """Upgrade legacy custom-group rows without deleting existing groups."""
    cursor = await database.execute("PRAGMA table_info(custom_group_players)")
    columns = {row["name"] for row in await cursor.fetchall()}

    if "guild_id" not in columns:
        await database.execute(
            "ALTER TABLE custom_group_players ADD COLUMN guild_id TEXT"
        )
    if "discord_user_id" not in columns:
        await database.execute(
            "ALTER TABLE custom_group_players ADD COLUMN discord_user_id TEXT"
        )

    # Backfill guild IDs for legacy username-only group members.
    await database.execute(
        """
        UPDATE custom_group_players
        SET guild_id = (
            SELECT custom_groups.guild_id
            FROM custom_groups
            WHERE custom_groups.id = custom_group_players.custom_group_id
        )
        WHERE guild_id IS NULL
        """
    )
    await database.commit()


async def create_custom_group(
    guild_id: int,
    name: str,
    tracked_player_count: int,
    created_by_discord_user_id: int,
    owner_proavalon_username: str,
):
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("Group name cannot be empty.")
    if len(clean_name) > 50:
        raise ValueError("Group names cannot be longer than 50 characters.")
    if tracked_player_count < 5 or tracked_player_count > 10:
        raise ValueError("Tracked game size must be between 5 and 10 players.")

    database = await connect()
    try:
        await _ensure_group_membership_schema(database)
        gid = _guild_id_text(guild_id)
        owner_id = str(created_by_discord_user_id)

        cursor = await database.execute(
            "SELECT id,name FROM custom_groups WHERE guild_id=? AND created_by_discord_user_id=? LIMIT 1",
            (gid, owner_id),
        )
        existing_owned = await cursor.fetchone()
        if existing_owned is not None:
            raise CustomGroupAlreadyExistsError(
                f"You already own the group `{existing_owned['name']}`. Each user may own only one group."
            )

        cursor = await database.execute(
            """
            SELECT g.name
            FROM custom_group_players p
            JOIN custom_groups g ON g.id=p.custom_group_id
            WHERE p.guild_id=? AND p.discord_user_id=?
            LIMIT 1
            """,
            (gid, owner_id),
        )
        membership = await cursor.fetchone()
        if membership is not None:
            raise CustomGroupMembershipError(
                f"You are already in `{membership['name']}`. A user may be in only one group at a time."
            )

        try:
            cursor = await database.execute(
                """
                INSERT INTO custom_groups (
                    guild_id,name,tracked_player_count,
                    created_by_discord_user_id,created_at
                ) VALUES (?,?,?,?,?)
                """,
                (gid, clean_name, tracked_player_count, owner_id, utc_now()),
            )
        except Exception as error:
            if "UNIQUE constraint failed" in str(error):
                raise CustomGroupAlreadyExistsError(
                    f"A custom group named `{clean_name}` already exists in this server."
                ) from error
            raise

        group_id = int(cursor.lastrowid)
        await database.execute(
            """
            INSERT INTO custom_group_players (
                custom_group_id,proavalon_username,added_by_discord_user_id,
                added_at,guild_id,discord_user_id
            ) VALUES (?,?,?,?,?,?)
            """,
            (
                group_id,owner_proavalon_username.strip(),owner_id,utc_now(),gid,owner_id,
            ),
        )
        await database.commit()
        return await _get_custom_group_by_id_with_db(database, guild_id, group_id)
    except Exception:
        await database.rollback()
        raise
    finally:
        await database.close()


async def _get_custom_group_by_id_with_db(database, guild_id:int, group_id:int):
    cursor = await database.execute(
        """
        SELECT id,guild_id,name,tracked_player_count,created_by_discord_user_id,created_at
        FROM custom_groups WHERE guild_id=? AND id=? LIMIT 1
        """,
        (_guild_id_text(guild_id),group_id),
    )
    return await cursor.fetchone()


async def get_custom_group_by_id(guild_id:int, group_id:int):
    database=await connect()
    try:
        await _ensure_group_membership_schema(database)
        return await _get_custom_group_by_id_with_db(database,guild_id,group_id)
    finally: await database.close()


async def get_custom_group_by_name(guild_id:int,name:str):
    database=await connect()
    try:
        await _ensure_group_membership_schema(database)
        cursor=await database.execute(
            """SELECT id,guild_id,name,tracked_player_count,created_by_discord_user_id,created_at
               FROM custom_groups WHERE guild_id=? AND name=? COLLATE NOCASE LIMIT 1""",
            (_guild_id_text(guild_id),name.strip()),
        )
        return await cursor.fetchone()
    finally: await database.close()


async def get_custom_groups(guild_id:int):
    database=await connect()
    try:
        await _ensure_group_membership_schema(database)
        cursor=await database.execute(
            """SELECT id,guild_id,name,tracked_player_count,created_by_discord_user_id,created_at
               FROM custom_groups WHERE guild_id=? ORDER BY name COLLATE NOCASE ASC""",
            (_guild_id_text(guild_id),),
        )
        return await cursor.fetchall()
    finally: await database.close()


async def get_custom_group_for_owner(guild_id:int, discord_user_id:int):
    database=await connect()
    try:
        await _ensure_group_membership_schema(database)
        cursor=await database.execute(
            """SELECT id,guild_id,name,tracked_player_count,created_by_discord_user_id,created_at
               FROM custom_groups WHERE guild_id=? AND created_by_discord_user_id=? LIMIT 1""",
            (_guild_id_text(guild_id),str(discord_user_id)),
        )
        return await cursor.fetchone()
    finally: await database.close()


async def get_custom_group_for_member(guild_id:int, discord_user_id:int):
    database=await connect()
    try:
        await _ensure_group_membership_schema(database)
        cursor=await database.execute(
            """SELECT g.id,g.guild_id,g.name,g.tracked_player_count,g.created_by_discord_user_id,g.created_at
               FROM custom_group_players p JOIN custom_groups g ON g.id=p.custom_group_id
               WHERE p.guild_id=? AND p.discord_user_id=? LIMIT 1""",
            (_guild_id_text(guild_id),str(discord_user_id)),
        )
        return await cursor.fetchone()
    finally: await database.close()


async def delete_custom_group(guild_id:int,group_id:int):
    group=await get_custom_group_by_id(guild_id,group_id)
    if group is None: return None
    database=await connect()
    try:
        await database.execute("DELETE FROM custom_group_players WHERE custom_group_id=?",(group_id,))
        await database.execute("DELETE FROM custom_groups WHERE guild_id=? AND id=?",(_guild_id_text(guild_id),group_id))
        await database.commit(); return group
    finally: await database.close()


async def add_custom_group_player(
    guild_id:int,
    custom_group_id:int,
    discord_user_id:int,
    proavalon_username:str,
    added_by_discord_user_id:int,
) -> None:
    canonical_username=proavalon_username.strip()
    if not canonical_username: raise ValueError("ProAvalon username cannot be empty.")
    database=await connect()
    try:
        await _ensure_group_membership_schema(database)
        gid=_guild_id_text(guild_id); target=str(discord_user_id)

        cursor=await database.execute(
            """SELECT g.name FROM custom_group_players p JOIN custom_groups g ON g.id=p.custom_group_id
               WHERE p.guild_id=? AND p.discord_user_id=? LIMIT 1""",
            (gid,target),
        )
        existing=await cursor.fetchone()
        if existing is not None:
            raise CustomGroupMembershipError(
                f"That user is already in `{existing['name']}`. A user may be in only one group at a time."
            )

        cursor=await database.execute("SELECT COUNT(*) AS total FROM custom_group_players WHERE custom_group_id=?",(custom_group_id,))
        if int((await cursor.fetchone())["total"]) >= 10:
            raise CustomGroupMembershipError("That group already has the maximum of 10 members.")

        try:
            await database.execute(
                """INSERT INTO custom_group_players
                   (custom_group_id,proavalon_username,added_by_discord_user_id,added_at,guild_id,discord_user_id)
                   VALUES (?,?,?,?,?,?)""",
                (custom_group_id,canonical_username,str(added_by_discord_user_id),utc_now(),gid,target),
            )
            await database.commit()
        except Exception as error:
            if "UNIQUE constraint failed" in str(error):
                raise CustomGroupPlayerAlreadyExistsError(
                    f"`{canonical_username}` is already registered in that group."
                ) from error
            raise
    finally: await database.close()


async def remove_custom_group_member(custom_group_id:int, discord_user_id:int):
    database=await connect()
    try:
        await _ensure_group_membership_schema(database)
        cursor=await database.execute(
            """SELECT id,custom_group_id,proavalon_username,discord_user_id,added_by_discord_user_id,added_at
               FROM custom_group_players WHERE custom_group_id=? AND discord_user_id=? LIMIT 1""",
            (custom_group_id,str(discord_user_id)),
        )
        player=await cursor.fetchone()
        if player is None: return None
        await database.execute("DELETE FROM custom_group_players WHERE id=?",(player["id"],))
        await database.commit(); return player
    finally: await database.close()


# Legacy username remover remains available for old rows without Discord IDs.
async def remove_custom_group_player(custom_group_id:int,proavalon_username:str):
    database=await connect()
    try:
        cursor=await database.execute(
            """SELECT id,custom_group_id,proavalon_username,added_by_discord_user_id,added_at,discord_user_id
               FROM custom_group_players WHERE custom_group_id=? AND proavalon_username=? COLLATE NOCASE LIMIT 1""",
            (custom_group_id,proavalon_username.strip()),
        )
        player=await cursor.fetchone()
        if player is None:return None
        await database.execute("DELETE FROM custom_group_players WHERE id=?",(player["id"],))
        await database.commit();return player
    finally:await database.close()


async def get_custom_group_players(custom_group_id:int):
    database=await connect()
    try:
        await _ensure_group_membership_schema(database)
        cursor=await database.execute(
            """SELECT id,custom_group_id,proavalon_username,added_by_discord_user_id,added_at,guild_id,discord_user_id
               FROM custom_group_players WHERE custom_group_id=? ORDER BY proavalon_username COLLATE NOCASE ASC""",
            (custom_group_id,),
        )
        return await cursor.fetchall()
    finally:await database.close()


async def count_custom_group_players(custom_group_id:int)->int:
    database=await connect()
    try:
        cursor=await database.execute("SELECT COUNT(*) AS total FROM custom_group_players WHERE custom_group_id=?",(custom_group_id,))
        return int((await cursor.fetchone())["total"])
    finally:await database.close()