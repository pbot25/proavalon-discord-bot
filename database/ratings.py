from dataclasses import dataclass
from datetime import datetime, timezone

from database.connection import connect


@dataclass(frozen=True)
class RatingSnapshot:
    username: str
    rating: int
    rating_bracket: str | None
    last_seen_online_at: str
    rating_updated_at: str


_rating_cache: dict[str, RatingSnapshot] = {}
_online_usernames: set[str] = set()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_cached_rating(
    username: str,
) -> RatingSnapshot | None:
    return _rating_cache.get(
        username.strip().casefold()
    )


def get_online_usernames() -> set[str]:
    """Return a copy of the usernames in the latest live lobby list."""

    return set(_online_usernames)


def is_player_online(
    username: str,
) -> bool:
    return (
        username.strip().casefold()
        in _online_usernames
    )


async def load_rating_cache() -> int:
    database = await connect()

    try:
        cursor = await database.execute(
            """
            SELECT
                proavalon_username,
                rating,
                rating_bracket,
                last_seen_online_at,
                rating_updated_at
            FROM proavalon_ratings
            """
        )
        rows = await cursor.fetchall()
    finally:
        await database.close()

    _rating_cache.clear()

    for row in rows:
        snapshot = RatingSnapshot(
            username=str(
                row["proavalon_username"]
            ),
            rating=int(row["rating"]),
            rating_bracket=(
                str(row["rating_bracket"])
                if row["rating_bracket"] is not None
                else None
            ),
            last_seen_online_at=str(
                row["last_seen_online_at"]
            ),
            rating_updated_at=str(
                row["rating_updated_at"]
            ),
        )
        _rating_cache[
            snapshot.username.casefold()
        ] = snapshot

    return len(_rating_cache)


async def upsert_online_ratings(
    players: list[dict],
) -> int:
    """
    Persist one update-current-players-list payload.

    A player disappearing from the online list does NOT delete their
    rating. Their last observed rating remains available until the bot
    sees them online again.
    """

    now = utc_now()
    cleaned: list[tuple[str, int, str | None]] = []

    for player in players:
        username = player.get("displayUsername")
        rating = player.get("playerRating")
        bracket = player.get("ratingBracket")

        if not isinstance(username, str):
            continue

        username = username.strip()

        if not username:
            continue

        if isinstance(rating, bool):
            continue

        try:
            rating_int = int(rating)
        except (TypeError, ValueError):
            continue

        bracket_text = (
            bracket.strip().casefold()
            if isinstance(bracket, str)
            and bracket.strip()
            else None
        )

        cleaned.append(
            (
                username,
                rating_int,
                bracket_text,
            )
        )

    _online_usernames.clear()
    _online_usernames.update(
        username.casefold()
        for username, _, _ in cleaned
    )

    if not cleaned:
        return 0

    database = await connect()

    try:
        await database.executemany(
            """
            INSERT INTO proavalon_ratings (
                proavalon_username,
                rating,
                rating_bracket,
                last_seen_online_at,
                rating_updated_at
            )
            VALUES (?, ?, ?, ?, ?)

            ON CONFLICT(proavalon_username)
            DO UPDATE SET
                rating = excluded.rating,
                rating_bracket = excluded.rating_bracket,
                last_seen_online_at = excluded.last_seen_online_at,
                rating_updated_at = excluded.rating_updated_at
            """,
            [
                (
                    username,
                    rating,
                    bracket,
                    now,
                    now,
                )
                for username, rating, bracket in cleaned
            ],
        )
        await database.commit()
    finally:
        await database.close()

    for username, rating, bracket in cleaned:
        _rating_cache[
            username.casefold()
        ] = RatingSnapshot(
            username=username,
            rating=rating,
            rating_bracket=bracket,
            last_seen_online_at=now,
            rating_updated_at=now,
        )

    return len(cleaned)
