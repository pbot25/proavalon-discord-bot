from __future__ import annotations


def clockwise_player_order(players: list[str]) -> list[str]:
    """Return the clockwise order used by ProAvalon's vote chart."""
    if not players:
        return []
    return [str(players[0]), *[str(value) for value in reversed(players[1:])]]


def m1_hammer_from_vote_history(
    players: list[str],
    vote_history: dict,
) -> str | None:
    """Infer M1's P5 leader even when an earlier proposal was approved."""
    ordered = clockwise_player_order(players)
    if not ordered or not isinstance(vote_history, dict):
        return None

    leaders: list[tuple[int, str]] = []
    proposal_count = 0
    for raw_name in players:
        history = (
            vote_history.get(raw_name)
            or vote_history.get(str(raw_name).casefold())
            or []
        )
        first_mission = (
            history[0] if history and isinstance(history[0], list) else []
        )
        proposal_count = max(proposal_count, len(first_mission))

    for proposal_index in range(proposal_count):
        leader = None
        for raw_name in players:
            history = (
                vote_history.get(raw_name)
                or vote_history.get(str(raw_name).casefold())
                or []
            )
            first_mission = (
                history[0] if history and isinstance(history[0], list) else []
            )
            if (
                proposal_index < len(first_mission)
                and "VHleader" in str(first_mission[proposal_index])
            ):
                leader = str(raw_name)
                break
        if leader is not None:
            leaders.append((proposal_index, leader))

    if not leaders:
        return None

    direction = 1
    if len(leaders) >= 2:
        first_index = ordered.index(leaders[0][1])
        second_index = ordered.index(leaders[1][1])
        delta = (second_index - first_index) % len(ordered)
        if delta == len(ordered) - 1:
            direction = -1

    proposal_index, leader = leaders[0]
    leader_index = ordered.index(leader)
    hammer_index = (
        leader_index + direction * (4 - proposal_index)
    ) % len(ordered)
    return ordered[hammer_index]
