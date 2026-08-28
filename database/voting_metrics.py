from __future__ import annotations

from dataclasses import dataclass, field
import json

from database.connection import connect


RESISTANCE_ROLES = {
    "resistance", "tristan", "isolde", "percival", "merlin",
    "melron", "moregano",
}
SPY_ROLES = {
    "spy", "assassin", "morgana", "mordred", "oberon",
    "hitberon", "mordredassassin",
}


def _role_key(value) -> str:
    return str(value or "").replace(" ", "").casefold()


def role_bucket(value) -> str:
    key = _role_key(value)
    if key == "resistance":
        return "VT"
    if key in {"tristan", "isolde"}:
        return "Lover"
    if key == "percival":
        return "Percival"
    if key in {"merlin", "melron"}:
        return "Merlin"
    # Keep unusual Resistance variants visible rather than silently losing data.
    return str(value or "Resistance")


@dataclass
class VotingLine:
    eligible_votes: int = 0
    correct_votes: int = 0
    crit_errors: int = 0

    @property
    def accuracy(self) -> float | None:
        if not self.eligible_votes:
            return None
        return self.correct_votes / self.eligible_votes * 100.0

    @property
    def error_rate(self) -> float | None:
        if not self.eligible_votes:
            return None
        return self.crit_errors / self.eligible_votes * 100.0


@dataclass
class PlayerVotingMetrics:
    username: str
    games_played: int = 0
    overall: VotingLine = field(default_factory=VotingLine)
    by_role: dict[str, VotingLine] = field(default_factory=dict)


def _token(history: dict, player: str, mission_index: int, pick_index: int) -> str:
    missions = history.get(player) or []
    if mission_index >= len(missions):
        return ""
    picks = missions[mission_index] or []
    if pick_index >= len(picks):
        return ""
    return str(picks[pick_index] or "")


def _proposal_count(history: dict, players: list[str], mission_index: int) -> int:
    return max(
        (
            len((history.get(player) or [])[mission_index] or [])
            for player in players
            if mission_index < len(history.get(player) or [])
        ),
        default=0,
    )


def _leader_for(history: dict, players: list[str], mission_index: int, pick_index: int):
    for player in players:
        if "VHleader" in _token(history, player, mission_index, pick_index):
            return player
    return None


def _picked_players(history: dict, players: list[str], mission_index: int, pick_index: int):
    return [
        player for player in players
        if "VHpicked" in _token(history, player, mission_index, pick_index)
    ]


def _is_approve(token: str) -> bool:
    return "VHapprove" in token


def _is_reject(token: str) -> bool:
    return "VHreject" in token


def _is_hammer(pick_index: int) -> bool:
    # ProAvalon vote history labels proposals P1..P5; P5 is hammer.
    return pick_index >= 4


def _remaining_rotation_has_resistance(
    players: list[str],
    roles_by_name: dict[str, str],
    leader: str | None,
    pick_index: int,
) -> bool:
    """Whether Resistance still has a leader opportunity at/after this proposal.

    The current Resistance leader counts as a meaningful opportunity. If the
    current leader is a Spy, at least one later pre-hammer leader must be Res.
    """
    if leader is None or leader not in players:
        return False

    leader_role = _role_key(roles_by_name.get(leader.casefold()))
    if leader_role in RESISTANCE_ROLES:
        return True

    remaining_after = max(0, 4 - pick_index)
    start = players.index(leader)
    for step in range(1, remaining_after + 1):
        candidate = players[(start + step) % len(players)]
        if _role_key(roles_by_name.get(candidate.casefold())) in RESISTANCE_ROLES:
            return True
    return False


def analyze_match(match: dict) -> tuple[dict[str, PlayerVotingMetrics], list[str]]:
    """Return per-player crit voting metrics and final-third-fail wrong voters."""
    players = list(match.get("players") or [])
    roles = list(match.get("roles") or [])
    history = match.get("vote_history") or {}
    missions = list(match.get("mission_history") or [])

    if not players or not isinstance(history, dict):
        return {}, []

    roles_by_name = {
        str(player).casefold(): (roles[i] if i < len(roles) else "")
        for i, player in enumerate(players)
    }
    metrics = {
        player.casefold(): PlayerVotingMetrics(player)
        for player in players
        if _role_key(roles_by_name.get(player.casefold())) in RESISTANCE_ROLES
    }

    failed_before = 0
    final_third_fail_wrong: list[str] = []

    for mission_index in range(max(len(missions), 5)):
        proposal_count = _proposal_count(history, players, mission_index)
        if proposal_count == 0:
            if mission_index < len(missions) and str(missions[mission_index]).casefold() == "failed":
                failed_before += 1
            continue

        is_crit = failed_before >= 2

        for pick_index in range(proposal_count):
            if not is_crit:
                continue

            leader = _leader_for(history, players, mission_index, pick_index)
            if not _remaining_rotation_has_resistance(
                players, roles_by_name, leader, pick_index
            ):
                continue

            picked = _picked_players(history, players, mission_index, pick_index)
            dirty = any(
                _role_key(roles_by_name.get(name.casefold())) in SPY_ROLES
                for name in picked
            )
            proposal_passed = (
                mission_index < len(missions)
                and pick_index == proposal_count - 1
            )

            leader_is_res = (
                leader is not None
                and _role_key(roles_by_name.get(leader.casefold())) in RESISTANCE_ROLES
            )

            # Is this the final Resistance-led opportunity before every later
            # pre-hammer leader is a Spy?
            later_res_exists = False
            if leader is not None and leader in players:
                remaining_after = max(0, 4 - pick_index)
                start = players.index(leader)
                for step in range(1, remaining_after + 1):
                    candidate = players[(start + step) % len(players)]
                    if _role_key(roles_by_name.get(candidate.casefold())) in RESISTANCE_ROLES:
                        later_res_exists = True
                        break
            last_res_opportunity = leader_is_res and not later_res_exists

            for player_key, result in metrics.items():
                player = result.username
                token = _token(history, player, mission_index, pick_index)
                approve = _is_approve(token)
                reject = _is_reject(token)
                if not approve and not reject:
                    continue

                # Explicit exception: approving hammer is not evaluated.
                if _is_hammer(pick_index) and approve:
                    continue

                result.overall.eligible_votes += 1
                bucket = role_bucket(roles_by_name.get(player_key))
                role_line = result.by_role.setdefault(bucket, VotingLine())
                role_line.eligible_votes += 1

                correct = (not dirty and approve) or (dirty and reject)
                if correct:
                    result.overall.correct_votes += 1
                    role_line.correct_votes += 1

                crit_error = False
                # Error type 1: help a dirty crit proposal actually go through.
                if dirty and approve and proposal_passed:
                    crit_error = True
                # Error type 2: reject the last clean Resistance-led proposal.
                if (not dirty) and reject and last_res_opportunity:
                    crit_error = True

                if crit_error:
                    result.overall.crit_errors += 1
                    role_line.crit_errors += 1

                    # Match-history callout: on the mission that becomes the
                    # Spies' third failed mission, identify Resistance approvers
                    # who were wrong on the dirty crit proposal that passed.
                    if (
                        dirty and approve and proposal_passed
                        and failed_before == 2
                        and mission_index < len(missions)
                        and str(missions[mission_index]).casefold() == "failed"
                        and player not in final_third_fail_wrong
                    ):
                        final_third_fail_wrong.append(player)

        if mission_index < len(missions) and str(missions[mission_index]).casefold() == "failed":
            failed_before += 1

    return metrics, final_third_fail_wrong


async def _season_match_dicts(season_id: int) -> list[dict]:
    db = await connect()
    try:
        cur = await db.execute(
            """
            SELECT players_json, roles_json, vote_history_json,
                   mission_history_json, fails_history_json
            FROM season_matches
            WHERE community_season_id = ?
            ORDER BY finished_at, id
            """,
            (int(season_id),),
        )
        rows = await cur.fetchall()
    finally:
        await db.close()

    result = []
    for row in rows:
        try:
            result.append({
                "players": json.loads(row["players_json"] or "[]"),
                "roles": json.loads(row["roles_json"] or "[]"),
                "vote_history": json.loads(row["vote_history_json"] or "{}"),
                "mission_history": json.loads(row["mission_history_json"] or "[]"),
                "fails_history": json.loads(row["fails_history_json"] or "[]"),
            })
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    return result


async def get_season_voting_metrics(season_id: int) -> dict[str, PlayerVotingMetrics]:
    combined: dict[str, PlayerVotingMetrics] = {}
    for match in await _season_match_dicts(season_id):
        # Total season games are counted for every appearance, regardless of
        # alignment. This gives /stats voting useful context for how Crit
        # Errors are distributed across the player's games.
        for player in match.get("players") or []:
            if not isinstance(player, str) or not player.strip():
                continue
            key = player.casefold()
            target = combined.setdefault(key, PlayerVotingMetrics(player))
            target.games_played += 1

        per_match, _ = analyze_match(match)
        for key, item in per_match.items():
            target = combined.setdefault(key, PlayerVotingMetrics(item.username))
            target.overall.eligible_votes += item.overall.eligible_votes
            target.overall.correct_votes += item.overall.correct_votes
            target.overall.crit_errors += item.overall.crit_errors
            for role_name, line in item.by_role.items():
                out = target.by_role.setdefault(role_name, VotingLine())
                out.eligible_votes += line.eligible_votes
                out.correct_votes += line.correct_votes
                out.crit_errors += line.crit_errors
    return combined


async def get_player_voting_metrics(season_id: int, username: str) -> PlayerVotingMetrics:
    all_metrics = await get_season_voting_metrics(season_id)
    return all_metrics.get(username.casefold(), PlayerVotingMetrics(username))


def final_third_fail_wrong_voters_from_live_data(game_data: dict) -> list[str]:
    match = {
        "players": game_data.get("playerUsernamesOrdered") or [],
        "roles": ((game_data.get("see") or {}).get("roles") or []),
        "vote_history": game_data.get("voteHistory") or {},
        "mission_history": game_data.get("missionHistory") or [],
        "fails_history": game_data.get("numFailsHistory") or [],
    }
    _, wrong = analyze_match(match)
    return wrong
