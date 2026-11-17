from __future__ import annotations

import json
import math
from datetime import datetime, timezone

from database.insights import unpack, winner_norm


def _timestamp(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _rating_changes(row) -> dict[str, float]:
    try:
        raw = row["rating_changes_json"]
    except (KeyError, IndexError):
        return {}
    try:
        values = raw if isinstance(raw, list) else json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return {}
    changes: dict[str, float] = {}
    for item in values if isinstance(values, list) else []:
        if not isinstance(item, dict) or not item.get("username"):
            continue
        change = item.get("change")
        if isinstance(change, bool) or not isinstance(change, (int, float)):
            continue
        if not math.isfinite(float(change)):
            continue
        changes[str(item["username"]).casefold()] = float(change)
    return changes


def build_activity_stats(
    rows,
    *,
    start_at: datetime,
    end_at: datetime,
) -> list[dict]:
    """Aggregate archived player activity inside [start_at, end_at)."""
    stats: dict[str, dict] = {}
    for row in rows:
        finished_at = _timestamp(row["finished_at"])
        if finished_at is None or not (start_at <= finished_at < end_at):
            continue
        winner = winner_norm(row["winner"])
        changes = _rating_changes(row)
        for participant in unpack(row):
            username = participant["username"]
            key = username.casefold()
            entry = stats.setdefault(
                key,
                {
                    "username": username,
                    "games": 0,
                    "wins": 0,
                    "losses": 0,
                    "win_rate": 0.0,
                    "mmr_change": 0.0,
                    "mmr_games": 0,
                    "first_game_at": finished_at,
                    "last_game_at": finished_at,
                },
            )
            entry["username"] = username
            entry["games"] += 1
            won = participant["alignment"] is not None and participant["alignment"] == winner
            entry["wins"] += int(won)
            entry["losses"] += int(not won)
            entry["first_game_at"] = min(entry["first_game_at"], finished_at)
            entry["last_game_at"] = max(entry["last_game_at"], finished_at)
            if key in changes:
                entry["mmr_change"] += changes[key]
                entry["mmr_games"] += 1

    results = list(stats.values())
    for entry in results:
        entry["win_rate"] = (
            entry["wins"] / entry["games"] * 100 if entry["games"] else 0.0
        )
        if float(entry["mmr_change"]).is_integer():
            entry["mmr_change"] = int(entry["mmr_change"])
    results.sort(
        key=lambda item: (
            -item["games"],
            -item["win_rate"],
            -item["mmr_change"],
            item["username"].casefold(),
        )
    )
    return results
