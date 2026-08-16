import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import aiohttp
from bs4 import BeautifulSoup


class ProAvalonError(Exception):
    """Base exception for ProAvalon requests."""


class ProAvalonLoginError(ProAvalonError):
    """Raised when authentication with ProAvalon fails."""


class ProAvalonUserNotFound(ProAvalonError):
    """Raised when a requested ProAvalon user cannot be found."""


class ProAvalonResponseError(ProAvalonError):
    """Raised when ProAvalon returns an unexpected response."""


@dataclass(frozen=True)
class RoleRecord:
    wins: int
    losses: int

    @property
    def games_played(self) -> int:
        return self.wins + self.losses

    @property
    def win_rate(self) -> float:
        if self.games_played == 0:
            return 0.0

        return self.wins / self.games_played

    @property
    def win_rate_percent(self) -> float:
        return round(self.win_rate * 100, 1)


@dataclass(frozen=True)
class PlayerProfile:
    username: str
    avatar_res_url: str | None
    avatar_spy_url: str | None
    nationality: str
    nation_code: str
    date_joined: str | None
    biography: str

    total_games_played: int
    total_wins: int
    total_losses: int
    total_time_played_raw: str | None

    total_res_wins: int
    total_res_losses: int

    role_stats: dict[str, dict[str, RoleRecord]]
    role_alliances: dict[str, str]
    rating_bracket: str | None

    @property
    def total_time_played_seconds(self) -> float | None:
        """
        Convert ProAvalon's totalTimePlayed timestamp-like duration into
        elapsed seconds.

        ProAvalon encodes durations relative to the Unix epoch. For
        example, 1970-01-02T10:18:10.097Z represents about 34h 18m.
        """

        if self.total_time_played_raw is None:
            return None

        raw_value = self.total_time_played_raw.strip()

        if not raw_value:
            return None

        try:
            parsed = datetime.fromisoformat(
                raw_value.replace("Z", "+00:00")
            )
        except ValueError:
            return None

        if parsed.tzinfo is None:
            parsed = parsed.replace(
                tzinfo=timezone.utc
            )
        else:
            parsed = parsed.astimezone(
                timezone.utc
            )

        epoch = datetime(
            1970,
            1,
            1,
            tzinfo=timezone.utc,
        )

        seconds = (
            parsed - epoch
        ).total_seconds()

        return max(
            0.0,
            seconds,
        )

    @property
    def total_time_played_hours(self) -> float | None:
        seconds = self.total_time_played_seconds

        if seconds is None:
            return None

        return seconds / 3600.0

    @property
    def total_win_rate(self) -> float:
        if self.total_games_played == 0:
            return 0.0

        return self.total_wins / self.total_games_played

    @property
    def total_win_rate_percent(self) -> float:
        return round(self.total_win_rate * 100, 1)

    @property
    def total_spy_wins(self) -> int:
        return self.total_wins - self.total_res_wins

    @property
    def total_spy_losses(self) -> int:
        return self.total_losses - self.total_res_losses

    @property
    def total_res_games(self) -> int:
        return self.total_res_wins + self.total_res_losses

    @property
    def total_spy_games(self) -> int:
        return self.total_spy_wins + self.total_spy_losses

    @property
    def resistance_win_rate_percent(self) -> float:
        if self.total_res_games == 0:
            return 0.0

        return round(
            self.total_res_wins / self.total_res_games * 100,
            1,
        )

    @property
    def spy_win_rate_percent(self) -> float:
        if self.total_spy_games == 0:
            return 0.0

        return round(
            self.total_spy_wins / self.total_spy_games * 100,
            1,
        )


class ProAvalonProfileScraper:
    BASE_URL = "https://proavalon.com"

    def __init__(
        self,
        minimum_delay_seconds: float = 3.0,
        maximum_attempts: int = 2,
    ) -> None:
        if minimum_delay_seconds < 0:
            raise ValueError(
                "minimum_delay_seconds cannot be negative."
            )

        if maximum_attempts < 1:
            raise ValueError(
                "maximum_attempts must be at least 1."
            )

        self._minimum_delay_seconds = minimum_delay_seconds
        self._maximum_attempts = maximum_attempts

        self._last_request_time = 0.0
        self._request_lock = asyncio.Lock()

        self._session: aiohttp.ClientSession | None = None
        self._logged_in = False

    async def __aenter__(self) -> "ProAvalonProfileScraper":
        await self.start()
        return self

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: Any,
    ) -> None:
        await self.close()

    async def start(self) -> None:
        """Create one reusable HTTP session."""

        if self._session is not None and not self._session.closed:
            return

        timeout = aiohttp.ClientTimeout(
            total=25,
            connect=10,
            sock_connect=10,
            sock_read=20,
        )

        self._session = aiohttp.ClientSession(
            timeout=timeout,
            cookie_jar=aiohttp.CookieJar(),
            headers={
                "User-Agent": (
                    "ProAvalon-Community-Stats-Bot/0.1 "
                    "(rate-limited community Discord bot)"
                ),
                "Accept": (
                    "text/html,application/xhtml+xml,"
                    "application/json;q=0.9,*/*;q=0.8"
                ),
            },
        )

    def get_authenticated_session(
        self,
    ) -> aiohttp.ClientSession:
        """
        Return the bot's logged-in ProAvalon HTTP session.

        Used by the rating tracker only to copy the authenticated
        session cookies into its Socket.IO connection.
        """

        session = self._require_started_session()

        if not self._logged_in:
            raise ProAvalonLoginError(
                "The ProAvalon session is not logged in."
            )

        return session

    async def close(self) -> None:
        """Close the HTTP session."""

        if self._session is not None:
            await self._session.close()

        self._session = None
        self._logged_in = False

    async def _wait_for_rate_limit(self) -> None:
        """
        Ensure requests are spaced apart.

        The lock prevents simultaneous Discord commands from passing
        through the rate limiter at the same time.
        """

        async with self._request_lock:
            now = time.monotonic()
            elapsed = now - self._last_request_time
            remaining = self._minimum_delay_seconds - elapsed

            if remaining > 0:
                await asyncio.sleep(remaining)

            self._last_request_time = time.monotonic()

    def _require_started_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            raise RuntimeError(
                "The scraper has not been started. Call start() first."
            )

        return self._session

    async def login(
        self,
        username: str,
        password: str,
    ) -> None:
        """
        Log into ProAvalon and retain its session cookie.

        The password is sent directly to ProAvalon and is not retained
        by this class.
        """

        session = self._require_started_session()

        clean_username = username.strip()

        if not clean_username:
            raise ProAvalonLoginError(
                "The ProAvalon username is missing."
            )

        if not password:
            raise ProAvalonLoginError(
                "The ProAvalon password is missing."
            )

        await self._wait_for_rate_limit()

        try:
            async with session.post(
                f"{self.BASE_URL}/login",
                data={
                    "username": clean_username,
                    "password": password,
                },
                allow_redirects=True,
            ) as response:
                html = await response.text()
                final_path = response.url.path.casefold()

                if response.status == 429:
                    raise ProAvalonLoginError(
                        "ProAvalon rate-limited the login request."
                    )

                if response.status >= 400:
                    raise ProAvalonLoginError(
                        f"ProAvalon returned HTTP {response.status} "
                        "during login."
                    )

        except ProAvalonError:
            raise
        except (
            aiohttp.ClientError,
            asyncio.TimeoutError,
        ) as error:
            raise ProAvalonLoginError(
                "Could not connect to ProAvalon during login."
            ) from error

        page_text = BeautifulSoup(
            html,
            "html.parser",
        ).get_text(" ", strip=True).casefold()

        successful_paths = {
            "/lobby",
            "/loginsuccess",
        }

        if (
            final_path not in successful_paths
            and not final_path.startswith("/lobby")
        ):
            self._logged_in = False
            raise ProAvalonLoginError(
                "ProAvalon login failed. Check the username and password."
            )

        if (
            "please log in to view this page" in page_text
            or "incorrect username" in page_text
            or "incorrect password" in page_text
            or "login failed" in page_text
        ):
            self._logged_in = False
            raise ProAvalonLoginError(
                "ProAvalon login failed. Check the username and password."
            )

        self._logged_in = True

    async def _get_profile_page_identity(
        self,
        username: str,
    ) -> tuple[str, str | None]:
        """
        Load the public profile page once and return the canonical
        username plus the visible rating bracket.
        """

        session = self._require_started_session()

        if not self._logged_in:
            raise ProAvalonLoginError(
                "Call login() before resolving usernames."
            )

        clean_username = username.strip()

        if not clean_username:
            raise ValueError("Username cannot be empty.")

        await self._wait_for_rate_limit()

        encoded_username = quote(clean_username, safe="")
        url = f"{self.BASE_URL}/profile/{encoded_username}"

        try:
            async with session.get(
                url,
                allow_redirects=True,
            ) as response:
                response_text = await response.text()

                if response.status == 404:
                    raise ProAvalonUserNotFound(
                        f"Could not find ProAvalon user "
                        f"'{clean_username}'."
                    )

                if response.status == 429:
                    raise ProAvalonError(
                        "ProAvalon rate-limited the username lookup."
                    )

                if response.status != 200:
                    raise ProAvalonResponseError(
                        f"ProAvalon returned HTTP {response.status}."
                    )

        except (
            aiohttp.ClientError,
            asyncio.TimeoutError,
        ) as error:
            raise ProAvalonError(
                "Could not connect to ProAvalon while resolving "
                "the username."
            ) from error

        if self._looks_like_login_page(response_text):
            self._logged_in = False
            raise ProAvalonLoginError(
                "The ProAvalon session expired."
            )

        soup = BeautifulSoup(response_text, "html.parser")
        username_element = soup.select_one("#profileUsername")

        if username_element is None:
            page_text = soup.get_text(" ", strip=True).casefold()

            if (
                "user does not exist" in page_text
                or "profile not found" in page_text
            ):
                raise ProAvalonUserNotFound(
                    f"Could not find ProAvalon user "
                    f"'{clean_username}'."
                )

            raise ProAvalonResponseError(
                "Could not determine the profile's canonical username."
            )

        canonical_username = username_element.get_text(
            " ",
            strip=True,
        )

        if not canonical_username:
            raise ProAvalonResponseError(
                "The profile page returned an empty username."
            )

        rating_bracket: str | None = None

        valid_brackets = {
            "iron",
            "bronze",
            "silver",
            "gold",
            "platinum",
            "diamond",
            "champion",
        }

        for row in soup.select("tr"):
            cells = row.find_all("td")

            if len(cells) < 2:
                continue

            label = cells[0].get_text(
                " ",
                strip=True,
            ).casefold()

            if label != "rank:":
                continue

            bracket_text = cells[1].get_text(
                " ",
                strip=True,
            )

            if bracket_text.casefold() in valid_brackets:
                rating_bracket = bracket_text.title()

            break

        return canonical_username, rating_bracket

    async def resolve_username_case(
        self,
        username: str,
    ) -> str:
        """
        Resolve a ProAvalon username case-insensitively.
        """

        canonical_username, _ = (
            await self._get_profile_page_identity(username)
        )

        return canonical_username

    async def get_profile_data(
        self,
        username: str,
    ) -> PlayerProfile:
        canonical_username, rating_bracket = (
            await self._get_profile_page_identity(username)
        )

        payload = await self.get_raw_profile_data(
            canonical_username
        )

        return parse_player_profile(
            payload,
            rating_bracket=rating_bracket,
        )

    async def get_raw_profile_data(
        self,
        username: str,
    ) -> dict[str, Any]:
        """
        Retrieve the original profile JSON from:

        /ajax/profile/getProfileData/<username>
        """

        self._require_started_session()

        if not self._logged_in:
            raise ProAvalonLoginError(
                "Call login() before requesting profile data."
            )

        clean_username = username.strip()

        if not clean_username:
            raise ValueError("Username cannot be empty.")

        encoded_username = quote(clean_username, safe="")
        url = (
            f"{self.BASE_URL}/ajax/profile/"
            f"getProfileData/{encoded_username}"
        )

        payload = await self._request_profile_payload(
            url=url,
            clean_username=clean_username,
        )

        user_data = payload.get("userData")

        if not isinstance(user_data, dict):
            raise ProAvalonUserNotFound(
                f"Could not find ProAvalon user '{clean_username}'."
            )

        returned_username = user_data.get("username")

        if not isinstance(returned_username, str):
            raise ProAvalonResponseError(
                "The profile response did not include a valid username."
            )

        return payload

    async def _request_profile_payload(
        self,
        url: str,
        clean_username: str,
    ) -> dict[str, Any]:
        """Request a profile, retrying once after transient network errors."""

        session = self._require_started_session()
        last_error: BaseException | None = None

        for attempt in range(1, self._maximum_attempts + 1):
            await self._wait_for_rate_limit()

            try:
                async with session.get(
                    url,
                    allow_redirects=True,
                ) as response:

                    response_text = await response.text()

                    if response.status == 404:
                        raise ProAvalonUserNotFound(
                            f"Could not find ProAvalon user "
                            f"'{clean_username}'."
                        )

                    if response.status == 429:
                        raise ProAvalonError(
                            "ProAvalon rate-limited the profile request."
                        )

                    if response.status != 200:
                        raise ProAvalonResponseError(
                            f"ProAvalon returned HTTP {response.status}."
                        )

                    if self._looks_like_login_page(response_text):
                        self._logged_in = False
                        raise ProAvalonLoginError(
                            "The ProAvalon session expired. "
                            "Restart the bot so it can log in again."
                        )

                    try:
                        payload = json.loads(response_text)
                    except json.JSONDecodeError as error:
                        raise ProAvalonResponseError(
                            "ProAvalon returned invalid JSON."
                        ) from error

                    if not isinstance(payload, dict):
                        raise ProAvalonResponseError(
                            "ProAvalon returned an unexpected response."
                        )

                    return payload

            except (
                ProAvalonUserNotFound,
                ProAvalonLoginError,
                ProAvalonResponseError,
            ):
                # These are not transient connection failures, so retrying
                # would not help.
                raise

            except aiohttp.ClientResponseError:
                raise

            except (
                aiohttp.ClientConnectionError,
                aiohttp.ServerDisconnectedError,
                aiohttp.ServerTimeoutError,
                asyncio.TimeoutError,
            ) as error:
                last_error = error
                
                if attempt < self._maximum_attempts:
                    # The next loop iteration also passes through the normal
                    # request rate limiter.
                    await asyncio.sleep(1.0)

            except aiohttp.ClientError as error:
                last_error = error

                if attempt < self._maximum_attempts:
                    await asyncio.sleep(1.0)

        raise ProAvalonError(
            "Could not connect to ProAvalon after "
            f"{self._maximum_attempts} attempts."
        ) from last_error

    @staticmethod
    def _looks_like_login_page(response_text: str) -> bool:
        page_text = BeautifulSoup(
            response_text,
            "html.parser",
        ).get_text(" ", strip=True).casefold()

        lowered_response = response_text.casefold()

        return (
            "please log in to view this page" in page_text
            or 'action="/login"' in lowered_response
            or "log in!" in page_text
        )


def parse_player_profile(
    payload: dict[str, Any],
    rating_bracket: str | None = None,
) -> PlayerProfile:
    """Convert raw ProAvalon profile JSON into a typed object."""

    user_data = payload.get("userData")
    role_alliances_raw = payload.get("roleAlliances", {})

    if not isinstance(user_data, dict):
        raise ProAvalonResponseError(
            "Profile data is missing from the response."
        )

    if not isinstance(role_alliances_raw, dict):
        role_alliances_raw = {}

    username = _required_string(
        user_data,
        "username",
    )

    role_stats = _parse_role_stats(
        user_data.get("roleStats", {})
    )

    role_alliances = {
        str(role): str(alignment)
        for role, alignment in role_alliances_raw.items()
    }

    return PlayerProfile(
        username=username,
        avatar_res_url=_optional_string(
            user_data.get("avatarImgRes")
        ),
        avatar_spy_url=_optional_string(
            user_data.get("avatarImgSpy")
        ),
        nationality=_string_or_empty(
            user_data.get("nationality")
        ),
        nation_code=_string_or_empty(
            user_data.get("nationCode")
        ),
        date_joined=_optional_string(
            user_data.get("dateJoined")
        ),
        biography=_string_or_empty(
            user_data.get("biography")
        ),
        total_games_played=_nonnegative_integer(
            user_data.get("totalGamesPlayed")
        ),
        total_wins=_nonnegative_integer(
            user_data.get("totalWins")
        ),
        total_losses=_nonnegative_integer(
            user_data.get("totalLosses")
        ),
        total_time_played_raw=_optional_string(
            user_data.get("totalTimePlayed")
        ),
        total_res_wins=_nonnegative_integer(
            user_data.get("totalResWins")
        ),
        total_res_losses=_nonnegative_integer(
            user_data.get("totalResLosses")
        ),
        role_stats=role_stats,
        role_alliances=role_alliances,
        rating_bracket=rating_bracket,
    )


def _parse_role_stats(
    raw_role_stats: Any,
) -> dict[str, dict[str, RoleRecord]]:
    if not isinstance(raw_role_stats, dict):
        return {}

    parsed: dict[str, dict[str, RoleRecord]] = {}

    for game_size, raw_roles in raw_role_stats.items():
        if not isinstance(raw_roles, dict):
            continue

        game_size_key = str(game_size)
        parsed[game_size_key] = {}

        for role_name, raw_record in raw_roles.items():
            if not isinstance(raw_record, dict):
                continue

            parsed[game_size_key][str(role_name)] = RoleRecord(
                wins=_nonnegative_integer(
                    raw_record.get("wins")
                ),
                losses=_nonnegative_integer(
                    raw_record.get("losses")
                ),
            )

    return parsed


def _required_string(
    mapping: dict[str, Any],
    key: str,
) -> str:
    value = mapping.get(key)

    if not isinstance(value, str) or not value.strip():
        raise ProAvalonResponseError(
            f"The profile response is missing '{key}'."
        )

    return value.strip()


def _optional_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None

    stripped = value.strip()

    if not stripped:
        return None

    return stripped


def _string_or_empty(value: Any) -> str:
    optional_value = _optional_string(value)

    if optional_value is None:
        return ""

    return optional_value


def _nonnegative_integer(value: Any) -> int:
    if isinstance(value, bool):
        return 0

    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0

    return max(number, 0)