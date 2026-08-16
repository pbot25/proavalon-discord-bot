from __future__ import annotations

import re
import discord

_DISCORD_ID_PATTERN = re.compile(r"^(?:<@!?)?(\d{15,22})>?$")

def parse_discord_user_id(value: str) -> int:
    cleaned = value.strip()
    match = _DISCORD_ID_PATTERN.fullmatch(cleaned)
    if match is None:
        raise ValueError("Enter a valid Discord user ID, for example `123456789012345678`.")
    user_id = int(match.group(1))
    if user_id <= 0:
        raise ValueError("Discord user ID must be positive.")
    return user_id

def resolve_discord_target(member: discord.Member | None, user_id_text: str | None) -> tuple[int, str]:
    if member is None and not user_id_text:
        raise ValueError("Provide either a Discord user (`@user`) or a Discord user ID.")
    if member is not None and user_id_text:
        raise ValueError("Choose only one: Discord user (`@user`) or Discord user ID.")
    if member is not None:
        return member.id, member.mention
    user_id = parse_discord_user_id(str(user_id_text))
    return user_id, f"<@{user_id}>"