import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from proavalon.profile_scraper import PlayerProfile


@dataclass
class CachedProfile:
    profile: PlayerProfile
    expires_at: float


class ProfileCache:
    """
    Keeps recently retrieved ProAvalon profiles in memory.

    A separate lock is used for each username so that two simultaneous
    Discord commands do not request the same profile twice.
    """

    def __init__(self, ttl_seconds: float = 15 * 60) -> None:
        if ttl_seconds <= 0:
            raise ValueError("Cache lifetime must be greater than zero.")

        self._ttl_seconds = ttl_seconds
        self._profiles: dict[str, CachedProfile] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def ttl_seconds(self) -> float:
        return self._ttl_seconds

    def get(self, username: str) -> PlayerProfile | None:
        key = username.strip().casefold()

        if not key:
            return None

        cached = self._profiles.get(key)

        if cached is None:
            return None

        if time.monotonic() >= cached.expires_at:
            del self._profiles[key]
            return None

        return cached.profile

    def set(
        self,
        username: str,
        profile: PlayerProfile,
    ) -> None:
        key = username.strip().casefold()

        if not key:
            raise ValueError("Username cannot be empty.")

        self._profiles[key] = CachedProfile(
            profile=profile,
            expires_at=time.monotonic() + self._ttl_seconds,
        )

    def remove(self, username: str) -> None:
        key = username.strip().casefold()
        self._profiles.pop(key, None)

    def clear(self) -> None:
        self._profiles.clear()

    async def get_or_fetch(
        self,
        username: str,
        fetcher: Callable[[str], Awaitable[PlayerProfile]],
    ) -> tuple[PlayerProfile, bool]:
        """
        Return a profile and whether it came from the cache.

        Returns:
            (profile, True) if cached
            (profile, False) if newly retrieved
        """

        clean_username = username.strip()

        if not clean_username:
            raise ValueError("Username cannot be empty.")

        cached = self.get(clean_username)

        if cached is not None:
            return cached, True

        key = clean_username.casefold()
        lock = self._locks.setdefault(key, asyncio.Lock())

        async with lock:
            # Check again because another command may have fetched it
            # while this command was waiting for the lock.
            cached = self.get(clean_username)

            if cached is not None:
                return cached, True

            profile = await fetcher(clean_username)
            self.set(profile.username, profile)

            return profile, False