from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
import json
from typing import Any

from database.connection import connect


MIN_SHARED_GAMES = 5
MIN_ROLE_PAIR_GAMES = 3

RESISTANCE_ROLES = {
    "resistance",
    "tristan",
    "isolde",
    "percival",
    "merlin",
    "melron",
    "moregano",
}

SPY_ROLES = {
    "spy",
    "assassin",
    "morgana",
    "mordred",
    "oberon",
    "hitberon",
    "mordredassassin",
}


@dataclass(slots=True)
class MatchParticipant:
    username: str
    role: str | None
    alignment: str | None


@dataclass(slots=True)
class ArchivedMatch:
    finished_at: str
    winner: str | None
    participants: list[MatchParticipant]


def _safe_json(value: Any, default):
    if value is None:
        return default

    if isinstance(value, (list, dict)):
        return value

    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def role_alignment(role: str | None) -> str | None:
    if not role:
        return None

    normalized = str(role).replace(" ", "").casefold()

    if normalized in RESISTANCE_ROLES:
        return "Resistance"

    if normalized in SPY_ROLES:
        return "Spy"

    return None


def _winner_normalized(winner: str | None) -> str | None:
    if not winner:
        return None

    normalized = str(winner).casefold()
    if "res" in normalized:
        return "Resistance"
    if "spy" in normalized:
        return "Spy"
    return None


def _player_won(
    participant: MatchParticipant,
    winner: str | None,
) -> bool | None:
    normalized_winner = _winner_normalized(winner)
    if participant.alignment is None or normalized_winner is None:
        return None
    return participant.alignment == normalized_winner


async def get_season_matches(
    community_season_id: int,
) -> list[ArchivedMatch]:
    db = await connect()
    try:
        cursor = await db.execute(
            """
            SELECT finished_at, winner, players_json, roles_json
            FROM season_matches
            WHERE community_season_id = ?
            ORDER BY finished_at ASC, id ASC
            """,
            (community_season_id,),
        )
        rows = await cursor.fetchall()
    finally:
        await db.close()

    matches: list[ArchivedMatch] = []

    for row in rows:
        players = _safe_json(row["players_json"], [])
        roles = _safe_json(row["roles_json"], [])

        if not isinstance(players, list):
            continue

        if not isinstance(roles, list):
            roles = []

        participants: list[MatchParticipant] = []

        for index, raw_username in enumerate(players):
            username = str(raw_username).strip()
            if not username:
                continue

            role = None
            if index < len(roles) and roles[index] is not None:
                role = str(roles[index])

            participants.append(
                MatchParticipant(
                    username=username,
                    role=role,
                    alignment=role_alignment(role),
                )
            )

        if not participants:
            continue

        matches.append(
            ArchivedMatch(
                finished_at=str(row["finished_at"]),
                winner=(
                    str(row["winner"])
                    if row["winner"] is not None
                    else None
                ),
                participants=participants,
            )
        )

    return matches


def _find_participant(
    match: ArchivedMatch,
    username: str,
) -> MatchParticipant | None:
    target = username.casefold()
    return next(
        (
            participant
            for participant in match.participants
            if participant.username.casefold() == target
        ),
        None,
    )


def _record_summary(stats: dict[str, Any]) -> dict[str, Any]:
    wins = int(stats.get("wins", 0))
    losses = int(stats.get("losses", 0))
    games = wins + losses
    return {
        **stats,
        "games": games,
        "win_rate": (wins / games * 100.0) if games else 0.0,
    }


def build_player_social_stats(
    matches: list[ArchivedMatch],
    username: str,
    *,
    minimum_shared_games: int = MIN_SHARED_GAMES,
) -> dict[str, Any] | None:
    target = username.casefold()
    relevant: list[ArchivedMatch] = []
    relationships: dict[str, dict[str, Any]] = {}
    role_pairs: dict[tuple[str, str, str], dict[str, Any]] = {}

    canonical_username = username

    for match in matches:
        player = _find_participant(match, username)
        if player is None:
            continue

        canonical_username = player.username
        relevant.append(match)
        player_won = _player_won(player, match.winner)

        for other in match.participants:
            if other.username.casefold() == target:
                continue

            key = other.username.casefold()
            rel = relationships.setdefault(
                key,
                {
                    "username": other.username,
                    "shared_games": 0,
                    "same_games": 0,
                    "opposite_games": 0,
                    "same_wins": 0,
                    "same_losses": 0,
                    "opponent_wins": 0,
                    "opponent_losses": 0,
                    "res_games": 0,
                    "res_wins": 0,
                    "res_losses": 0,
                    "spy_games": 0,
                    "spy_wins": 0,
                    "spy_losses": 0,
                },
            )

            rel["shared_games"] += 1

            same_alignment = (
                player.alignment is not None
                and other.alignment is not None
                and player.alignment == other.alignment
            )
            opposite_alignment = (
                player.alignment is not None
                and other.alignment is not None
                and player.alignment != other.alignment
            )

            if same_alignment:
                rel["same_games"] += 1
                if player_won is True:
                    rel["same_wins"] += 1
                elif player_won is False:
                    rel["same_losses"] += 1

                if player.alignment == "Resistance":
                    rel["res_games"] += 1
                    if player_won is True:
                        rel["res_wins"] += 1
                    elif player_won is False:
                        rel["res_losses"] += 1

                elif player.alignment == "Spy":
                    rel["spy_games"] += 1
                    if player_won is True:
                        rel["spy_wins"] += 1
                    elif player_won is False:
                        rel["spy_losses"] += 1

            elif opposite_alignment:
                rel["opposite_games"] += 1
                if player_won is True:
                    rel["opponent_wins"] += 1
                elif player_won is False:
                    rel["opponent_losses"] += 1

            if player.role and other.role:
                relation = (
                    "same"
                    if same_alignment
                    else "opposite"
                    if opposite_alignment
                    else "unknown"
                )
                pair_key = (
                    other.username.casefold(),
                    player.role.casefold(),
                    other.role.casefold(),
                )
                pair = role_pairs.setdefault(
                    pair_key,
                    {
                        "partner": other.username,
                        "player_role": player.role,
                        "partner_role": other.role,
                        "relation": relation,
                        "games": 0,
                        "wins": 0,
                        "losses": 0,
                    },
                )
                pair["games"] += 1
                if player_won is True:
                    pair["wins"] += 1
                elif player_won is False:
                    pair["losses"] += 1

    if not relevant:
        return None

    enriched = []
    for rel in relationships.values():
        shared = rel["shared_games"]
        same = rel["same_games"]
        opposite = rel["opposite_games"]
        rel["same_pct"] = (same / shared * 100.0) if shared else 0.0
        rel["opposite_pct"] = (
            opposite / shared * 100.0
            if shared
            else 0.0
        )
        rel["same_win_rate"] = (
            rel["same_wins"] / same * 100.0
            if same
            else 0.0
        )
        rel["opponent_win_rate"] = (
            rel["opponent_wins"] / opposite * 100.0
            if opposite
            else 0.0
        )
        rel["res_win_rate"] = (
            rel["res_wins"] / rel["res_games"] * 100.0
            if rel["res_games"]
            else 0.0
        )
        rel["spy_win_rate"] = (
            rel["spy_wins"] / rel["spy_games"] * 100.0
            if rel["spy_games"]
            else 0.0
        )
        enriched.append(rel)

    most_played = max(
        enriched,
        key=lambda rel: (
            rel["shared_games"],
            rel["username"].casefold(),
        ),
        default=None,
    )

    shared_eligible = [
        rel
        for rel in enriched
        if rel["shared_games"] >= minimum_shared_games
    ]
    teammate_eligible = [
        rel
        for rel in enriched
        if rel["same_games"] >= minimum_shared_games
    ]
    opponent_eligible = [
        rel
        for rel in enriched
        if rel["opposite_games"] >= minimum_shared_games
    ]
    res_eligible = [
        rel
        for rel in enriched
        if rel["res_games"] >= minimum_shared_games
    ]
    spy_eligible = [
        rel
        for rel in enriched
        if rel["spy_games"] >= minimum_shared_games
    ]

    best_teammate = max(
        teammate_eligible,
        key=lambda rel: (
            rel["same_win_rate"],
            rel["same_games"],
            rel["same_wins"],
        ),
        default=None,
    )
    worst_teammate = min(
        teammate_eligible,
        key=lambda rel: (
            rel["same_win_rate"],
            -rel["same_games"],
            -rel["same_losses"],
        ),
        default=None,
    )
    most_allied = max(
        shared_eligible,
        key=lambda rel: (
            rel["same_pct"],
            rel["shared_games"],
        ),
        default=None,
    )
    most_opposing = max(
        shared_eligible,
        key=lambda rel: (
            rel["opposite_pct"],
            rel["shared_games"],
        ),
        default=None,
    )
    nemesis = max(
        opponent_eligible,
        key=lambda rel: (
            rel["opponent_losses"],
            100.0 - rel["opponent_win_rate"],
            rel["opposite_games"],
        ),
        default=None,
    )
    most_dominated = max(
        opponent_eligible,
        key=lambda rel: (
            rel["opponent_wins"],
            rel["opponent_win_rate"],
            rel["opposite_games"],
        ),
        default=None,
    )
    best_res_partner = max(
        res_eligible,
        key=lambda rel: (
            rel["res_win_rate"],
            rel["res_games"],
        ),
        default=None,
    )
    best_spy_partner = max(
        spy_eligible,
        key=lambda rel: (
            rel["spy_win_rate"],
            rel["spy_games"],
        ),
        default=None,
    )

    role_pair_list = []
    for pair in role_pairs.values():
        games = pair["games"]
        pair["win_rate"] = (
            pair["wins"] / games * 100.0
            if games
            else 0.0
        )
        role_pair_list.append(pair)

    role_pair_list.sort(
        key=lambda pair: (
            pair["games"],
            pair["win_rate"],
        ),
        reverse=True,
    )

    return {
        "username": canonical_username,
        "tracking_since": relevant[0].finished_at,
        "tracking_through": relevant[-1].finished_at,
        "matches_analyzed": len(relevant),
        "minimum_shared_games": minimum_shared_games,
        "relationships": enriched,
        "most_played": most_played,
        "best_teammate": best_teammate,
        "worst_teammate": worst_teammate,
        "most_allied": most_allied,
        "most_opposing": most_opposing,
        "nemesis": nemesis,
        "most_dominated": most_dominated,
        "best_res_partner": best_res_partner,
        "best_spy_partner": best_spy_partner,
        "role_pairs": role_pair_list,
    }


def build_pair_social_stats(
    matches: list[ArchivedMatch],
    player_one: str,
    player_two: str,
) -> dict[str, Any] | None:
    p1_target = player_one.casefold()
    p2_target = player_two.casefold()

    relevant = []
    p1_name = player_one
    p2_name = player_two

    same_games = same_wins = same_losses = 0
    opposite_games = p1_opponent_wins = p2_opponent_wins = 0
    res_games = res_wins = res_losses = 0
    spy_games = spy_wins = spy_losses = 0
    role_pairs: dict[tuple[str, str], dict[str, Any]] = {}

    for match in matches:
        p1 = next(
            (
                participant
                for participant in match.participants
                if participant.username.casefold() == p1_target
            ),
            None,
        )
        p2 = next(
            (
                participant
                for participant in match.participants
                if participant.username.casefold() == p2_target
            ),
            None,
        )

        if p1 is None or p2 is None:
            continue

        p1_name = p1.username
        p2_name = p2.username
        relevant.append(match)

        p1_won = _player_won(p1, match.winner)
        p2_won = _player_won(p2, match.winner)

        same_alignment = (
            p1.alignment is not None
            and p2.alignment is not None
            and p1.alignment == p2.alignment
        )
        opposite_alignment = (
            p1.alignment is not None
            and p2.alignment is not None
            and p1.alignment != p2.alignment
        )

        if same_alignment:
            same_games += 1
            if p1_won is True:
                same_wins += 1
            elif p1_won is False:
                same_losses += 1

            if p1.alignment == "Resistance":
                res_games += 1
                if p1_won is True:
                    res_wins += 1
                elif p1_won is False:
                    res_losses += 1

            elif p1.alignment == "Spy":
                spy_games += 1
                if p1_won is True:
                    spy_wins += 1
                elif p1_won is False:
                    spy_losses += 1

        elif opposite_alignment:
            opposite_games += 1
            if p1_won is True:
                p1_opponent_wins += 1
            if p2_won is True:
                p2_opponent_wins += 1

        if p1.role and p2.role:
            key = (p1.role.casefold(), p2.role.casefold())
            record = role_pairs.setdefault(
                key,
                {
                    "player_one_role": p1.role,
                    "player_two_role": p2.role,
                    "relation": (
                        "same"
                        if same_alignment
                        else "opposite"
                        if opposite_alignment
                        else "unknown"
                    ),
                    "games": 0,
                    "player_one_wins": 0,
                    "player_two_wins": 0,
                    "shared_wins": 0,
                    "shared_losses": 0,
                },
            )
            record["games"] += 1

            if same_alignment:
                if p1_won is True:
                    record["shared_wins"] += 1
                elif p1_won is False:
                    record["shared_losses"] += 1
            else:
                if p1_won is True:
                    record["player_one_wins"] += 1
                if p2_won is True:
                    record["player_two_wins"] += 1

    if not relevant:
        return None

    role_pair_list = sorted(
        role_pairs.values(),
        key=lambda record: record["games"],
        reverse=True,
    )

    total = len(relevant)

    return {
        "player_one": p1_name,
        "player_two": p2_name,
        "tracking_since": relevant[0].finished_at,
        "tracking_through": relevant[-1].finished_at,
        "shared_games": total,
        "same_games": same_games,
        "same_pct": (same_games / total * 100.0) if total else 0.0,
        "same_wins": same_wins,
        "same_losses": same_losses,
        "same_win_rate": (
            same_wins / same_games * 100.0
            if same_games
            else 0.0
        ),
        "opposite_games": opposite_games,
        "opposite_pct": (
            opposite_games / total * 100.0
            if total
            else 0.0
        ),
        "player_one_opponent_wins": p1_opponent_wins,
        "player_two_opponent_wins": p2_opponent_wins,
        "res_games": res_games,
        "res_wins": res_wins,
        "res_losses": res_losses,
        "res_win_rate": (
            res_wins / res_games * 100.0
            if res_games
            else 0.0
        ),
        "spy_games": spy_games,
        "spy_wins": spy_wins,
        "spy_losses": spy_losses,
        "spy_win_rate": (
            spy_wins / spy_games * 100.0
            if spy_games
            else 0.0
        ),
        "role_pairs": role_pair_list,
    }
