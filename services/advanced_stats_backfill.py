from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import json
import re

import discord
from PIL import Image

from database.connection import connect
from database.insights import alignment


MATCH_TITLE = re.compile(
    r"^(?P<season>.+?)\s+[—-]\s+Match\s+#(?P<match>\d+)\s+Result$",
    re.IGNORECASE,
)
ROOM_FOOTER = re.compile(r"Room\s+(?P<room>\d+)", re.IGNORECASE)
BACKTICK_NAME = re.compile(r"`([^`]+)`")


@dataclass(slots=True)
class BackfillResult:
    database_matches: int = 0
    messages_scanned: int = 0
    matches_found: int = 0
    shots_recovered: int = 0
    ref_histories_recovered: int = 0
    hammers_recovered: int = 0
    already_complete: int = 0
    unmatched_messages: int = 0


def _json_list(value) -> list:
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value) if value else []
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _json_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value) if value else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _normalize(value: str) -> str:
    return " ".join(str(value or "").split()).casefold()


def _field(embed: discord.Embed, name: str) -> str | None:
    target = name.casefold()
    for item in embed.fields:
        if str(item.name).strip().casefold() == target:
            return str(item.value)
    return None


def _line_column(image: Image.Image, x: int) -> bool:
    if x < 0 or x >= image.width:
        return False
    hits = 0
    for y in range(48, min(70, image.height)):
        red, green, blue = image.getpixel((x, y))[:3]
        if max(red, green, blue) < 175:
            hits += 1
    return hits >= 16


def hammer_from_vote_chart(
    image_bytes: bytes,
    players: list[str],
) -> str | None:
    """Read the M1/P5 leader border from this bot's deterministic PNG."""
    try:
        image = Image.open(BytesIO(image_bytes)).convert("RGB")
    except Exception:
        return None
    if image.height < 74 + 42 * len(players) or image.width < 300:
        return None

    name_width = next(
        (
            x for x in range(60, image.width - 52)
            if _line_column(image, x) and _line_column(image, x + 52)
        ),
        None,
    )
    if name_width is None:
        return None

    fifth_proposal_x = name_width + (4 * 52)
    if fifth_proposal_x + 52 >= image.width:
        return None

    ordered = [players[0], *reversed(players[1:])] if players else []
    for row_index, username in enumerate(ordered):
        y = 74 + row_index * 42
        # A leader cell has a three-pixel #222 border two pixels inside the
        # ordinary grid. The regular cell border and picked-player X do not
        # produce a long dark run at this position.
        dark = 0
        for x in range(fifth_proposal_x + 2, fifth_proposal_x + 50):
            red, green, blue = image.getpixel((x, y + 2))[:3]
            if max(red, green, blue) < 70:
                dark += 1
        if dark >= 30:
            return str(username)
    return None


async def _match_index(guild_id: int):
    database = await connect()
    try:
        cursor = await database.execute(
            """
            SELECT sm.*, cs.display_name, cs.required_prefix
            FROM season_matches AS sm
            JOIN community_seasons AS cs ON cs.id = sm.community_season_id
            WHERE cs.guild_id = ?
            """,
            (str(guild_id),),
        )
        rows = await cursor.fetchall()
    finally:
        await database.close()

    by_title: dict[tuple[str, int], object] = {}
    by_room: dict[int, list] = {}
    for row in rows:
        names = {
            _normalize(row["display_name"]),
            _normalize(row["required_prefix"]),
        }
        for name in names:
            if name:
                by_title[(name, int(row["season_match_id"]))] = row
        if row["room_id"] is not None:
            by_room.setdefault(int(row["room_id"]), []).append(row)
    return by_title, by_room, len(rows)


async def _update_match(
    database_id: int,
    *,
    shots: list[str] | None,
    ref_history: list[str] | None,
    hammer_username: str | None,
    hammer_alignment: str | None,
) -> None:
    database = await connect()
    try:
        cursor = await database.execute(
            "SELECT card_history_json FROM season_matches WHERE id = ?",
            (database_id,),
        )
        current = await cursor.fetchone()
        cards = _json_dict(current["card_history_json"] if current else None)
        if ref_history:
            cards["Ref of the Rain"] = ref_history
        await database.execute(
            """
            UPDATE season_matches
            SET assassin_shots_json = COALESCE(?, assassin_shots_json),
                card_history_json = CASE WHEN ? THEN ? ELSE card_history_json END,
                m1_hammer_username = COALESCE(?, m1_hammer_username),
                m1_hammer_alignment = COALESCE(?, m1_hammer_alignment)
            WHERE id = ?
            """,
            (
                json.dumps(shots) if shots else None,
                1 if ref_history else 0,
                json.dumps(cards),
                hammer_username,
                hammer_alignment,
                database_id,
            ),
        )
        await database.commit()
    finally:
        await database.close()


async def backfill_advanced_stats(
    guild_id: int,
    channel: discord.TextChannel,
) -> BackfillResult:
    result = BackfillResult()
    by_title, by_room, result.database_matches = await _match_index(guild_id)

    async for message in channel.history(limit=None, oldest_first=True):
        result.messages_scanned += 1
        if not message.embeds:
            continue
        embed = message.embeds[0]
        title_match = MATCH_TITLE.match(str(embed.title or "").strip())
        footer_text = str(embed.footer.text or "") if embed.footer else ""
        room_match = ROOM_FOOTER.search(footer_text)

        row = None
        if title_match:
            row = by_title.get((
                _normalize(title_match.group("season")),
                int(title_match.group("match")),
            ))
        if row is None and room_match:
            room_rows = by_room.get(int(room_match.group("room")), [])
            if len(room_rows) == 1:
                row = room_rows[0]
        if row is None:
            if title_match or room_match:
                result.unmatched_messages += 1
            continue

        result.matches_found += 1
        existing_shots = _json_list(row["assassin_shots_json"])
        existing_cards = _json_dict(row["card_history_json"])
        existing_hammer = row["m1_hammer_username"]

        shot_value = _field(embed, "Shot")
        shots = BACKTICK_NAME.findall(shot_value or "")
        ref_value = _field(embed, "Ref of the Rain")
        ref_history = BACKTICK_NAME.findall(ref_value or "")

        players = [str(value) for value in _json_list(row["players_json"])]
        roles = _json_list(row["roles_json"])
        hammer_username = None
        if not existing_hammer and players:
            for attachment in message.attachments:
                if not (
                    str(attachment.filename).casefold().endswith(".png")
                    or str(attachment.content_type or "").casefold() == "image/png"
                ):
                    continue
                try:
                    hammer_username = hammer_from_vote_chart(
                        await attachment.read(), players
                    )
                except discord.DiscordException:
                    continue
                if hammer_username:
                    break

        hammer_alignment = None
        if hammer_username:
            for index, username in enumerate(players):
                if username.casefold() == hammer_username.casefold():
                    hammer_alignment = alignment(
                        roles[index] if index < len(roles) else None
                    )
                    break

        new_shots = shots if shots and not existing_shots else None
        has_existing_ref = any(
            "ref" in str(name).casefold() and "rain" in str(name).casefold()
            for name in existing_cards
        )
        new_ref = ref_history if ref_history and not has_existing_ref else None
        new_hammer = hammer_username if hammer_username and not existing_hammer else None

        if not any((new_shots, new_ref, new_hammer)):
            result.already_complete += 1
            continue
        await _update_match(
            int(row["id"]),
            shots=new_shots,
            ref_history=new_ref,
            hammer_username=new_hammer,
            hammer_alignment=hammer_alignment if new_hammer else None,
        )
        result.shots_recovered += int(bool(new_shots))
        result.ref_histories_recovered += int(bool(new_ref))
        result.hammers_recovered += int(bool(new_hammer))

    return result
