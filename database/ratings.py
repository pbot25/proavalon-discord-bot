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
        await database.executemany(
            """
            INSERT INTO proavalon_rating_extremes(
                proavalon_username, minimum_rating, maximum_rating, updated_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(proavalon_username) DO UPDATE SET
                minimum_rating = MIN(minimum_rating, excluded.minimum_rating),
                maximum_rating = MAX(maximum_rating, excluded.maximum_rating),
                updated_at = excluded.updated_at
            """,
            [(username, rating, rating, now) for username, rating, _ in cleaned],
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


async def upsert_observed_rating(
    username: str,
    rating: int,
    rating_bracket: str | None = None,
) -> RatingSnapshot:
    """Persist a direct profile observation without marking the user online."""
    clean_username = username.strip()
    now = utc_now()
    previous = get_cached_rating(clean_username)
    bracket = (
        rating_bracket.strip().casefold()
        if isinstance(rating_bracket, str) and rating_bracket.strip()
        else previous.rating_bracket if previous is not None else None
    )
    last_seen = (
        previous.last_seen_online_at if previous is not None else now
    )
    database = await connect()
    try:
        await database.execute(
            """
            INSERT INTO proavalon_ratings(
                proavalon_username, rating, rating_bracket,
                last_seen_online_at, rating_updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(proavalon_username) DO UPDATE SET
                rating = excluded.rating,
                rating_bracket = COALESCE(
                    excluded.rating_bracket,
                    proavalon_ratings.rating_bracket
                ),
                rating_updated_at = excluded.rating_updated_at
            """,
            (clean_username, int(rating), bracket, last_seen, now),
        )
        await database.execute(
            """
            INSERT INTO proavalon_rating_extremes(
                proavalon_username, minimum_rating, maximum_rating, updated_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(proavalon_username) DO UPDATE SET
                minimum_rating = MIN(minimum_rating, excluded.minimum_rating),
                maximum_rating = MAX(maximum_rating, excluded.maximum_rating),
                updated_at = excluded.updated_at
            """,
            (clean_username, int(rating), int(rating), now),
        )
        await database.commit()
    finally:
        await database.close()

    snapshot = RatingSnapshot(
        username=clean_username,
        rating=int(rating),
        rating_bracket=bracket,
        last_seen_online_at=last_seen,
        rating_updated_at=now,
    )
    _rating_cache[clean_username.casefold()] = snapshot
    return snapshot


async def get_rating_extremes(username: str) -> tuple[int | None, int | None]:
    """Return extremes merged from live observations and archived matches."""
    database = await connect()
    try:
        cursor = await database.execute(
            """
            SELECT minimum_rating, maximum_rating
            FROM proavalon_rating_extremes
            WHERE proavalon_username = ? COLLATE NOCASE
            """,
            (username.strip(),),
        )
        row = await cursor.fetchone()
        values: list[int] = []
        if row is not None:
            values.extend(
                [int(row["minimum_rating"]), int(row["maximum_rating"])]
            )

        # Always merge archived before/after ratings. This ensures a new live
        # observation cannot hide a pre-deployment season high or low.
        cursor = await database.execute(
            "SELECT rating_changes_json FROM season_matches "
            "WHERE rating_changes_json IS NOT NULL"
        )
        target = username.strip().casefold()
        for match in await cursor.fetchall():
            try:
                changes = __import__("json").loads(match["rating_changes_json"])
            except (TypeError, ValueError):
                continue
            for change in changes if isinstance(changes, list) else []:
                if str(change.get("username", "")).casefold() != target:
                    continue
                for key in ("before", "after"):
                    value = change.get(key)
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        values.append(int(value))
        if not values:
            return None, None

        minimum = min(values)
        maximum = max(values)
        now = utc_now()
        await database.execute(
            """
            INSERT INTO proavalon_rating_extremes(
                proavalon_username, minimum_rating, maximum_rating, updated_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(proavalon_username) DO UPDATE SET
                minimum_rating = MIN(minimum_rating, excluded.minimum_rating),
                maximum_rating = MAX(maximum_rating, excluded.maximum_rating),
                updated_at = excluded.updated_at
            """,
            (username.strip(), minimum, maximum, now),
        )
        await database.commit()
        return minimum, maximum
    finally:
        await database.close()
