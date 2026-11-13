from __future__ import annotations

from dataclasses import dataclass, field
import json

from database.connection import connect


ROLE_BUCKETS = ("VT", "Lover", "Percival", "Merlin")


def _safe_list(value) -> list:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def assassination_role_bucket(role) -> str | None:
    key = str(role or "").replace(" ", "").casefold()
    if key == "resistance":
        return "VT"
    if key in {"tristan", "isolde"}:
        return "Lover"
    if key in {"percival", "moregano"}:
        return "Percival"
    if key in {"merlin", "melron"}:
        return "Merlin"
    return None


@dataclass(slots=True)
class MerlinShotStats:
    username: str
    games_by_role: dict[str, int] = field(
        default_factory=lambda: {role: 0 for role in ROLE_BUCKETS}
    )
    shots_by_role: dict[str, int] = field(
        default_factory=lambda: {role: 0 for role in ROLE_BUCKETS}
    )

    @property
    def non_merlin_games(self) -> int:
        return sum(
            self.games_by_role[role]
            for role in ("VT", "Lover", "Percival")
        )

    @property
    def false_merlin_shots(self) -> int:
        return sum(
            self.shots_by_role[role]
            for role in ("VT", "Lover", "Percival")
        )

    @property
    def false_shot_rate(self) -> float:
        if not self.non_merlin_games:
            return 0.0
        return self.false_merlin_shots / self.non_merlin_games * 100.0

    @property
    def total_shots(self) -> int:
        return sum(self.shots_by_role.values())


async def get_season_merlin_shot_stats(
    community_season_id: int,
) -> dict[str, MerlinShotStats]:
    """Reconstruct assassination-target statistics from archived matches."""
    database = await connect()
    try:
        cursor = await database.execute(
            """
            SELECT players_json, roles_json, assassin_shots_json
            FROM season_matches
            WHERE community_season_id = ?
            ORDER BY finished_at ASC, id ASC
            """,
            (community_season_id,),
        )
        rows = await cursor.fetchall()
    finally:
        await database.close()

    results: dict[str, MerlinShotStats] = {}
    for row in rows:
        players = _safe_list(row["players_json"])
        roles = _safe_list(row["roles_json"])
        shots = {
            str(name).strip().casefold()
            for name in _safe_list(row["assassin_shots_json"])
            if isinstance(name, str) and name.strip()
        }

        for index, raw_username in enumerate(players):
            username = str(raw_username or "").strip()
            if not username:
                continue
            role = roles[index] if index < len(roles) else None
            bucket = assassination_role_bucket(role)
            if bucket is None:
                continue

            key = username.casefold()
            item = results.setdefault(key, MerlinShotStats(username=username))
            item.games_by_role[bucket] += 1
            if key in shots:
                item.shots_by_role[bucket] += 1

    return results


async def get_player_merlin_shot_stats(
    community_season_id: int,
    username: str,
) -> MerlinShotStats:
    all_stats = await get_season_merlin_shot_stats(community_season_id)
    return all_stats.get(
        username.casefold(),
        MerlinShotStats(username=username),
    )
