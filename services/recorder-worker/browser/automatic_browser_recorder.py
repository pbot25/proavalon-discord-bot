from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import time
from pathlib import Path

import aiohttp
from dotenv import load_dotenv
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from browser_recorder import (
    DEFAULT_VIEWPORT,
    PROAVALON_BASE_URL,
    convert_webm_to_mp4,
    dismiss_known_popups,
    force_recorder_preferences,
    join_room_as_spectator,
    lock_recording_tabs,
    verify_layout,
)
from recording_lifecycle import RecordingLifecycle, SessionState


SOCKET_URL = "wss://proavalon.com/socket.io/?EIO=4&transport=websocket"


class PbotBridgeClient:
    """Read pbot's authenticated live-lobby snapshots from Railway."""

    def __init__(self, url: str, secret: str, guild_id: int, recorder_id: str) -> None:
        self.url = url.rstrip("/") + "/recorder/lobbies"
        self.secret = secret
        self.guild_id = guild_id
        self.recorder_id = recorder_id
        self.session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=5),
            headers={"Authorization": f"Bearer {self.secret}"},
        )

    async def close(self) -> None:
        if self.session is not None:
            await self.session.close()
            self.session = None

    async def snapshot(self) -> dict:
        if self.session is None:
            raise RuntimeError("Pbot bridge session is not running.")
        async with self.session.get(
            self.url,
            params={"guild_id": str(self.guild_id)},
        ) as response:
            if response.status != 200:
                raise RuntimeError(
                    f"Pbot bridge returned HTTP {response.status}."
                )
            return await response.json()

    async def active_season_prefix(self) -> str | None:
        payload = await self.snapshot()
        active_season = payload.get("active_season") or {}
        prefix = str(active_season.get("required_prefix") or "").strip()
        return prefix or None

    async def _claim_request(self, method: str, suffix: str, room_id: int) -> dict:
        if self.session is None:
            raise RuntimeError("Pbot bridge session is not running.")
        async with self.session.request(
            method,
            self.url.replace("/recorder/lobbies", suffix),
            json={
                "guild_id": self.guild_id,
                "room_id": room_id,
                "recorder_id": self.recorder_id,
            },
        ) as response:
            if response.status != 200:
                raise RuntimeError(f"Pbot claim service returned HTTP {response.status}.")
            return await response.json()

    async def claim_room(self, room_id: int) -> bool:
        result = await self._claim_request("POST", "/recorder/claims", room_id)
        return bool(result.get("claimed"))

    async def heartbeat_room(self, room_id: int) -> bool:
        result = await self._claim_request(
            "POST", "/recorder/claims/heartbeat", room_id
        )
        if result.get("claimed"):
            return True
        # Pbot restarts clear its in-memory leases. Reclaim the same room
        # instead of interrupting an otherwise healthy recording.
        return await self.claim_room(room_id)

    async def release_room(self, room_id: int) -> None:
        await self._claim_request("DELETE", "/recorder/claims", room_id)

    async def audit(self, room_id: int, stage: str, detail: str | None = None) -> None:
        if self.session is None:
            return
        try:
            async with self.session.post(
                self.url.replace("/recorder/lobbies", "/recorder/audit"),
                json={
                    "guild_id": self.guild_id,
                    "room_id": room_id,
                    "recorder_id": self.recorder_id,
                    "stage": stage,
                    "detail": detail,
                },
            ) as response:
                if response.status != 200:
                    print(f"Recording audit returned HTTP {response.status}.")
        except (aiohttp.ClientError, asyncio.TimeoutError):
            # Audit reporting must never interrupt a recording.
            pass

    async def find_replacement(
        self,
        *,
        old_room_id: int,
        old_players: list[str],
        season_prefix: str,
        minimum_overlap: int,
    ) -> dict | None:
        if self.session is None:
            return None
        try:
            payload = await self.snapshot()
        except (
            aiohttp.ClientError,
            asyncio.TimeoutError,
            RuntimeError,
            ValueError,
        ):
            return None

        previous = {name.casefold() for name in old_players}
        prefix = season_prefix.casefold()
        claims = {
            int(claim["room_id"]): str(claim.get("recorder_id") or "")
            for claim in payload.get("room_claims") or []
            if claim.get("room_id") is not None
        }
        for lobby in payload.get("lobbies", []):
            try:
                room_id = int(lobby.get("room_id"))
                count = int(lobby.get("player_count", 0))
                maximum = int(lobby.get("max_players", 0))
            except (TypeError, ValueError):
                continue
            players = [
                name for name in lobby.get("players", [])
                if isinstance(name, str)
            ]
            status = str(lobby.get("status") or "").casefold()
            overlap = previous.intersection(name.casefold() for name in players)
            if (
                room_id != old_room_id
                and claims.get(room_id, self.recorder_id) == self.recorder_id
                and count == 10
                and maximum == 10
                and not lobby.get("password_locked", False)
                and status != "finished"
                and len(players) == 10
                and all(name.casefold().startswith(prefix) for name in players)
                and len(overlap) >= minimum_overlap
            ):
                return {
                    "room_id": room_id,
                    "players": players,
                    "overlap": sorted(overlap),
                }
        return None


class LobbyWatcher:
    """Authenticated listener for public lobby counts and room rosters."""

    def __init__(self, username: str, password: str) -> None:
        self.username = username
        self.password = password
        self.games: dict[int, dict] = {}
        self.rosters: dict[int, list[str]] = {}
        self.ten_player_breaks: dict[int, int] = {}
        self.finished_rooms: set[int] = set()
        self.current_room: int | None = None
        self.connected = asyncio.Event()
        self.changed = asyncio.Event()
        self.websocket = None
        self.session: aiohttp.ClientSession | None = None
        self.task: asyncio.Task | None = None

    async def start(self) -> None:
        self.task = asyncio.create_task(self._run(), name="proavalon-lobby-watcher")
        await asyncio.wait_for(self.connected.wait(), timeout=25)

    def raise_if_stopped(self) -> None:
        if self.task is None or not self.task.done():
            return
        exception = self.task.exception()
        if exception is not None:
            raise RuntimeError("The ProAvalon lobby watcher stopped.") from exception
        raise RuntimeError("The ProAvalon lobby watcher disconnected.")

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            except Exception:
                # The selected-room browser may intentionally replace this
                # account's watcher socket before cleanup reaches this point.
                pass

    def browser_cookies(self) -> list[dict]:
        if self.session is None:
            raise RuntimeError("The lobby watcher is not authenticated.")
        cookies = []
        for cookie in self.session.cookie_jar:
            domain = cookie["domain"] or "proavalon.com"
            path = cookie["path"] or "/"
            cookies.append(
                {
                    "name": cookie.key,
                    "value": cookie.value,
                    "domain": domain,
                    "path": path,
                    "secure": True,
                }
            )
        if not cookies:
            raise RuntimeError(
                "ProAvalon login succeeded but returned no reusable session cookie."
            )
        return cookies

    async def emit(self, event: str, value) -> None:
        if self.websocket is not None and not self.websocket.closed:
            payload = json.dumps([event, value], separators=(",", ":"))
            try:
                await self.websocket.send_str("42" + payload)
            except (aiohttp.ClientError, ConnectionError, RuntimeError):
                if not self.websocket.closed:
                    raise

    async def inspect_roster(self, room_id: int) -> list[str]:
        self.rosters.pop(room_id, None)
        self.current_room = room_id
        await self.emit("join-room", room_id)
        deadline = time.monotonic() + 3
        try:
            while time.monotonic() < deadline:
                roster = self.rosters.get(room_id)
                if roster:
                    return roster
                self.changed.clear()
                try:
                    await asyncio.wait_for(self.changed.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass
            return []
        finally:
            await self.emit("leave-room", "")
            self.current_room = None

    async def wait_for_qualifying_room(
        self,
        prefix: str,
        preferred_room_id: int | None = None,
        prefix_provider=None,
        room_claim=None,
        room_release=None,
        claim_delay_seconds: float = 0,
    ) -> tuple[int, list[str], int, str]:
        inspected: set[int] = set()
        claim_delay_pending = claim_delay_seconds
        current_prefix = prefix
        normalized_prefix = current_prefix.casefold()
        next_prefix_check = 0.0
        print(f"Waiting for a public 10-player `{current_prefix}` lobby...")
        while True:
            self.raise_if_stopped()
            now = time.monotonic()
            if prefix_provider is not None and now >= next_prefix_check:
                next_prefix_check = now + 5.0
                try:
                    dynamic_prefix = await prefix_provider()
                except (
                    aiohttp.ClientError,
                    asyncio.TimeoutError,
                    RuntimeError,
                    ValueError,
                ):
                    dynamic_prefix = current_prefix
                if (
                    dynamic_prefix is None
                    and normalized_prefix
                ):
                    current_prefix = ""
                    normalized_prefix = ""
                    inspected.clear()
                    print(
                        "Pbot has no active season; lobby recording is paused."
                    )
                elif (
                    dynamic_prefix is not None
                    and dynamic_prefix.casefold() != normalized_prefix
                ):
                    current_prefix = dynamic_prefix
                    normalized_prefix = current_prefix.casefold()
                    inspected.clear()
                    print(
                        "Active season changed; now waiting for a public "
                        f"10-player `{current_prefix}` lobby..."
                    )
            if not normalized_prefix:
                self.changed.clear()
                try:
                    await asyncio.wait_for(self.changed.wait(), timeout=1)
                except asyncio.TimeoutError:
                    pass
                continue
            live_ids = set(self.games)
            inspected.intersection_update(live_ids)
            games = list(self.games.items())
            if preferred_room_id is not None:
                games.sort(key=lambda item: item[0] != preferred_room_id)
            for room_id, game in games:
                try:
                    count = int(game.get("numOfPlayersInside", 0))
                    maximum = int(game.get("maxNumPlayers", 0))
                except (TypeError, ValueError):
                    continue
                status = str(game.get("status") or "").casefold()
                if count != 10:
                    inspected.discard(room_id)
                    continue
                if (
                    maximum != 10
                    or game.get("passwordLocked", False)
                    or status in {"game in progress", "finished"}
                    or room_id in inspected
                ):
                    continue
                if room_claim is not None:
                    if claim_delay_pending:
                        await asyncio.sleep(claim_delay_pending)
                        claim_delay_pending = 0
                    if not await room_claim(room_id):
                        inspected.discard(room_id)
                        continue
                inspected.add(room_id)
                roster = await self.inspect_roster(room_id)
                if len(roster) == 10 and all(
                    name.casefold().startswith(normalized_prefix)
                    for name in roster
                ):
                    print(
                        f"Room {room_id} reached ten qualifying players: "
                        + ", ".join(roster)
                    )
                    # Stay subscribed to the selected room. The global games
                    # list may remove a completed room before publishing a
                    # visible Finished status, while the room's final
                    # game-data payload reliably carries phase=finished.
                    self.finished_rooms.discard(room_id)
                    self.current_room = room_id
                    await self.emit("join-room", room_id)
                    return (
                        room_id,
                        roster,
                        self.ten_player_breaks.get(room_id, 0),
                        current_prefix,
                    )
                if room_release is not None:
                    await room_release(room_id)

            self.changed.clear()
            try:
                await asyncio.wait_for(self.changed.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass

    def room_snapshot(self, room_id: int) -> dict:
        return dict(self.games.get(room_id) or {})

    def room_finished(self, room_id: int) -> bool:
        return room_id in self.finished_rooms

    async def release_room(self, room_id: int) -> None:
        if self.current_room == room_id:
            await self.emit("leave-room", "")
            self.current_room = None
        self.finished_rooms.discard(room_id)

    def remained_at_ten(self, room_id: int, break_token: int) -> bool:
        game = self.games.get(room_id)
        if not game:
            return False
        try:
            count = int(game.get("numOfPlayersInside", 0))
        except (TypeError, ValueError):
            return False
        return (
            count == 10
            and self.ten_player_breaks.get(room_id, 0) == break_token
        )

    async def _run(self) -> None:
        timeout = aiohttp.ClientTimeout(
            total=None, connect=20, sock_connect=20, sock_read=None
        )
        async with aiohttp.ClientSession(
            timeout=timeout,
            cookie_jar=aiohttp.CookieJar(),
            headers={"User-Agent": "Mozilla/5.0 ProAvalon-Browser-Recorder"},
        ) as session:
            self.session = session
            print("Authenticating the recorder without opening a browser lobby...")
            async with session.post(
                f"{PROAVALON_BASE_URL}/login",
                data={"username": self.username, "password": self.password},
                allow_redirects=True,
            ) as response:
                response_text = (await response.text()).casefold()
                final_path = response.url.path.casefold()
                valid_path = (
                    final_path in {"/lobby", "/loginsuccess"}
                    or final_path.startswith("/lobby")
                )
                rejected = any(
                    marker in response_text
                    for marker in (
                        "incorrect username",
                        "incorrect password",
                        "login failed",
                        "please log in to view this page",
                    )
                )
                if response.status >= 400 or not valid_path or rejected:
                    raise RuntimeError(
                        "Direct ProAvalon recorder login failed. Check the "
                        "dedicated account credentials."
                    )

            print("Direct recorder authentication successful.")
            socket_headers = {"Origin": PROAVALON_BASE_URL}
            async with session.ws_connect(
                SOCKET_URL,
                heartbeat=None,
                autoping=True,
                headers=socket_headers,
            ) as websocket:
                self.websocket = websocket
                async for message in websocket:
                    if message.type != aiohttp.WSMsgType.TEXT:
                        continue
                    data = message.data
                    if data.startswith("0"):
                        await websocket.send_str("40")
                        continue
                    if data == "2":
                        if websocket.closed:
                            break
                        try:
                            await websocket.send_str("3")
                        except (aiohttp.ClientError, ConnectionError, RuntimeError):
                            if websocket.closed:
                                break
                            raise
                        continue
                    if data.startswith("40"):
                        self.connected.set()
                        continue
                    if not data.startswith("42"):
                        continue
                    try:
                        payload = json.loads(data[2:])
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(payload, list) or len(payload) < 2:
                        continue
                    event, event_data = payload[0], payload[1]
                    if event == "update-current-games-list" and isinstance(event_data, list):
                        previous_games = self.games
                        updated_games = {
                            int(game["roomId"]): game
                            for game in event_data
                            if isinstance(game, dict) and game.get("roomId") is not None
                        }
                        for room_id, previous in previous_games.items():
                            try:
                                was_ten = int(previous.get("numOfPlayersInside", 0)) == 10
                                is_ten = int(
                                    (updated_games.get(room_id) or {}).get(
                                        "numOfPlayersInside", 0
                                    )
                                ) == 10
                            except (TypeError, ValueError):
                                was_ten, is_ten = False, False
                            if was_ten and not is_ten:
                                self.ten_player_breaks[room_id] = (
                                    self.ten_player_breaks.get(room_id, 0) + 1
                                )
                        self.games = updated_games
                        self.changed.set()
                    elif (
                        event == "update-room-players"
                        and isinstance(event_data, list)
                        and self.current_room is not None
                    ):
                        names = [
                            player.get("username")
                            for player in event_data
                            if isinstance(player, dict)
                            and isinstance(player.get("username"), str)
                        ]
                        self.rosters[self.current_room] = names
                        self.changed.set()
                    elif event == "game-data" and isinstance(event_data, dict):
                        try:
                            room_id = int(event_data.get("roomId"))
                        except (TypeError, ValueError):
                            continue
                        if str(event_data.get("phase") or "").casefold() == "finished":
                            self.finished_rooms.add(room_id)
                            self.changed.set()


class BrowserGameMonitor:
    """Read lifecycle events from the recording page's own WebSocket."""

    def __init__(self) -> None:
        self.games: dict[int, dict] = {}
        self.finished_rooms: set[int] = set()
        self.changed = asyncio.Event()
        self.received_game_list = False

    def attach(self, page) -> None:
        page.on("websocket", self._on_websocket)

    def _on_websocket(self, websocket) -> None:
        websocket.on("framereceived", self._on_frame)

    def _on_frame(self, payload) -> None:
        if isinstance(payload, bytes):
            try:
                payload = payload.decode("utf-8")
            except UnicodeDecodeError:
                return
        if not isinstance(payload, str) or not payload.startswith("42"):
            return
        try:
            message = json.loads(payload[2:])
        except json.JSONDecodeError:
            return
        if not isinstance(message, list) or len(message) < 2:
            return
        event, event_data = message[0], message[1]
        if event == "update-current-games-list" and isinstance(event_data, list):
            self.received_game_list = True
            self.games = {
                int(game["roomId"]): game
                for game in event_data
                if isinstance(game, dict) and game.get("roomId") is not None
            }
            self.changed.set()
        elif event == "game-data" and isinstance(event_data, dict):
            try:
                room_id = int(event_data.get("roomId"))
            except (TypeError, ValueError):
                return
            if str(event_data.get("phase") or "").casefold() == "finished":
                self.finished_rooms.add(room_id)
                self.changed.set()

    def seed_game(self, room_id: int, game: dict) -> None:
        if game:
            self.games[room_id] = dict(game)

    def room_snapshot(self, room_id: int) -> dict:
        return dict(self.games.get(room_id) or {})

    def room_finished(self, room_id: int) -> bool:
        return room_id in self.finished_rooms

    async def wait_until_observing(self, room_id: int, timeout: float = 10) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if (
                self.received_game_list
                and (room_id in self.games or room_id in self.finished_rooms)
            ):
                return
            self.changed.clear()
            try:
                await asyncio.wait_for(self.changed.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                pass
        raise RuntimeError(
            "The recording browser did not receive live state for the selected room."
        )


def parse_args():
    # Load file-based defaults before constructing argparse options. Reading
    # RECORDER_SEASON_PREFIX earlier caused a valid .env value to be ignored.
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="Automatically record the next qualifying ProAvalon season lobby."
    )
    parser.add_argument(
        "--season-prefix",
        default=os.getenv("RECORDER_SEASON_PREFIX", ""),
        help="Required username prefix, for example S4",
    )
    parser.add_argument("--output-dir", default="recordings")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--keep-webm", action="store_true")
    parser.add_argument("--postgame-seconds", type=float, default=30.0)
    parser.add_argument("--abandonment-grace-seconds", type=float, default=120.0)
    parser.add_argument("--abandonment-player-threshold", type=int, default=4)
    parser.add_argument("--replacement-player-overlap", type=int, default=3)
    args = parser.parse_args()
    args.username = os.getenv("RECORDER_PROAVALON_USERNAME", "").strip()
    args.password = os.getenv("RECORDER_PROAVALON_PASSWORD", "")
    args.bridge_url = os.getenv("RECORDER_BRIDGE_URL", "").strip()
    args.bridge_secret = os.getenv("RECORDER_BRIDGE_SECRET", "").strip()
    args.recorder_id = os.getenv("RECORDER_INSTANCE_ID", "recorder-1").strip()
    try:
        args.claim_delay_seconds = max(
            0.0, float(os.getenv("RECORDER_CLAIM_DELAY_SECONDS", "0"))
        )
    except ValueError:
        parser.error("RECORDER_CLAIM_DELAY_SECONDS must be a number.")
    guild_id = os.getenv("DISCORD_GUILD_ID", "").strip()
    args.guild_id = int(guild_id) if guild_id.isdigit() else None
    args.season_prefix = args.season_prefix.strip()
    if not args.username or not args.password:
        parser.error("Set recorder username and password in .env.")
    if args.postgame_seconds < 0 or args.abandonment_grace_seconds <= 0:
        parser.error("Invalid postgame or abandonment duration.")
    if not 0 <= args.abandonment_player_threshold <= 9:
        parser.error("--abandonment-player-threshold must be between 0 and 9.")
    if not 1 <= args.replacement_player_overlap <= 10:
        parser.error("--replacement-player-overlap must be between 1 and 10.")
    if bool(args.bridge_url) != bool(args.bridge_secret):
        parser.error("Set both RECORDER_BRIDGE_URL and RECORDER_BRIDGE_SECRET.")
    if args.bridge_url and args.guild_id is None:
        parser.error("Set DISCORD_GUILD_ID when using the Pbot bridge.")
    if args.bridge_url and not args.recorder_id:
        parser.error("Set RECORDER_INSTANCE_ID when using the Pbot bridge.")
    if not args.bridge_url and not args.season_prefix:
        parser.error(
            "Configure the Pbot bridge or set RECORDER_SEASON_PREFIX as a fallback."
        )
    args.output_dir = Path(args.output_dir).resolve()
    return args


async def finalize_recording(
    webm_path: Path,
    mp4_path: Path,
    *,
    trim_start_seconds: float,
    duration_seconds: float,
    keep_webm: bool,
    bridge: PbotBridgeClient | None = None,
    room_id: int | None = None,
) -> None:
    converting_path = mp4_path.with_name(
        f"{mp4_path.stem}.converting{mp4_path.suffix}"
    )
    try:
        converting_path.unlink(missing_ok=True)
        converted = await asyncio.to_thread(
            convert_webm_to_mp4,
            webm_path,
            converting_path,
            trim_start_seconds=trim_start_seconds,
            duration_seconds=duration_seconds,
        )
        if not converted:
            converting_path.unlink(missing_ok=True)
            print(
                "Background FFmpeg conversion failed; raw recording retained at "
                f"{webm_path}."
            )
            return
        converting_path.replace(mp4_path)
        if not keep_webm:
            webm_path.unlink(missing_ok=True)
        print(f"Completed automatic recording: {mp4_path}")
        if bridge is not None and room_id is not None:
            await bridge.audit(room_id, "browser_mp4_completed", str(mp4_path))
    except Exception as error:
        converting_path.unlink(missing_ok=True)
        print(
            f"Background recording conversion error: {error}. "
            f"Raw recording retained at {webm_path}."
        )
        if bridge is not None and room_id is not None:
            await bridge.audit(room_id, "browser_conversion_failed", str(error))


async def record_selected_room(
    playwright,
    watcher: LobbyWatcher,
    args,
    room_id: int,
    break_token: int,
    cookies: list[dict],
    original_roster: list[str],
    bridge: PbotBridgeClient | None,
    conversion_tasks: set[asyncio.Task],
):
    output_dir: Path = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    scratch = output_dir / "_playwright_video"
    scratch.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    base = f"proavalon-room-{room_id}-{timestamp}"
    screenshot_path = output_dir / f"{base}-layout-check.png"
    webm_path = output_dir / f"{base}.webm"
    mp4_path = output_dir / f"{base}.mp4"

    browser = await playwright.chromium.launch(
        headless=not args.headed,
        args=["--disable-dev-shm-usage", "--no-sandbox"],
    )
    context = await browser.new_context(
        viewport=DEFAULT_VIEWPORT,
        record_video_dir=str(scratch),
        record_video_size=DEFAULT_VIEWPORT,
    )
    await context.add_cookies(cookies)
    capture_started_at = time.monotonic()
    page = await context.new_page()
    video = page.video
    browser_monitor = BrowserGameMonitor()
    browser_monitor.attach(page)
    lifecycle = RecordingLifecycle(
        postgame_seconds=args.postgame_seconds,
        abandonment_grace_seconds=args.abandonment_grace_seconds,
        abandonment_player_threshold=args.abandonment_player_threshold,
    )
    lifecycle.start()
    successful = False
    ready_at = None
    game_started_at = None
    replacement = None
    next_bridge_check = 0.0
    try:
        if bridge is not None:
            await bridge.audit(room_id, "browser_session_opening")
        print("Opening the authenticated ProAvalon recorder session...")
        await page.goto(
            f"{PROAVALON_BASE_URL}/lobby",
            wait_until="domcontentloaded",
        )
        try:
            await page.wait_for_selector("#current-games-table", timeout=20_000)
        except Exception as error:
            raise RuntimeError(
                "The direct login session could not be transferred to the "
                "recording browser. Refusing to log in a second time because "
                "that would disconnect the lobby watcher."
            ) from error
        print("Recorder browser reused the authenticated session.")
        await force_recorder_preferences(context, page)
        await join_room_as_spectator(page, room_id)
        await dismiss_known_popups(page)
        await lock_recording_tabs(page)
        await verify_layout(page)
        await page.screenshot(path=str(screenshot_path), full_page=False)
        ready_at = time.monotonic()
        browser_monitor.seed_game(room_id, watcher.room_snapshot(room_id))
        await browser_monitor.wait_until_observing(room_id)
        preparation_invalid = False
        browser_game = browser_monitor.room_snapshot(room_id)
        try:
            browser_count = int(browser_game.get("numOfPlayersInside", 0))
        except (TypeError, ValueError):
            browser_count = 0
        if browser_count != 10:
            preparation_invalid = True
        if preparation_invalid:
            lifecycle.update(
                now=ready_at,
                room_present=bool(watcher.room_snapshot(room_id)),
                player_count=9,
                game_in_progress=False,
                game_finished=False,
            )
            print("The lobby dropped below ten while the recorder was preparing.")
        else:
            print("Automatic recording is active.")
            if bridge is not None:
                await bridge.audit(room_id, "browser_ready")

            last_state = lifecycle.state
            while True:
                game = browser_monitor.room_snapshot(room_id)
                present = bool(game)
                try:
                    count = int(game.get("numOfPlayersInside")) if present else None
                except (TypeError, ValueError):
                    count = None
                status = str(game.get("status") or "").casefold()
                state = lifecycle.update(
                    now=time.monotonic(),
                    room_present=present,
                    player_count=count,
                    game_in_progress=status == "game in progress",
                    game_finished=(
                        status == "finished"
                        or browser_monitor.room_finished(room_id)
                    ),
                )
                now = time.monotonic()
                if (
                    bridge is not None
                    and state in {
                        SessionState.RECORDING_PREGAME,
                        SessionState.RECORDING_GAME,
                        SessionState.POSTGAME,
                    }
                    and now >= next_bridge_check
                ):
                    next_bridge_check = now + 1.0
                    if not await bridge.heartbeat_room(room_id):
                        raise RuntimeError(
                            f"Recorder lost its Pbot claim for room {room_id}."
                        )
                    if not lifecycle.game_started:
                        await asyncio.sleep(0.25)
                        continue
                    replacement = await bridge.find_replacement(
                        old_room_id=room_id,
                        old_players=original_roster,
                        season_prefix=args.season_prefix,
                        minimum_overlap=args.replacement_player_overlap,
                    )
                    if replacement is not None:
                        names = ", ".join(replacement["overlap"])
                        print(
                            f"Replacement lobby {replacement['room_id']} detected "
                            f"with {len(replacement['overlap'])} returning players: {names}"
                        )
                        if state is SessionState.POSTGAME:
                            # The match is already valid and must never be
                            # discarded merely because its players quickly
                            # formed another lobby. End only the optional
                            # postgame tail, save this match, then hand off.
                            lifecycle.state = SessionState.COMPLETED
                            successful = True
                            print(
                                "Ending postgame early, saving the completed "
                                "game, and handing off immediately."
                            )
                        else:
                            lifecycle.state = SessionState.ABANDONED
                            print(
                                "Discarding the unfinished old game and "
                                "handing off immediately."
                            )
                        state = lifecycle.state
                if state is not last_state:
                    print(f"Recording state: {last_state.value} -> {state.value}")
                    if state is SessionState.RECORDING_GAME:
                        game_started_at = time.monotonic()
                        print("Retained recording timeline started with the game.")
                        if bridge is not None:
                            await bridge.audit(room_id, "game_recording_started")
                    last_state = state
                if state is SessionState.COMPLETED:
                    successful = True
                    break
                if state in {
                    SessionState.CANCELLED_BEFORE_START,
                    SessionState.ABANDONED,
                }:
                    break
                await asyncio.sleep(0.25)
    finally:
        stopped_at = time.monotonic()
        try:
            await context.close()
        except PlaywrightError:
            # A discontinued room or crashed target can close the page/context
            # first. Cleanup must remain safe and must not mask the lifecycle
            # result with "Target ... has been closed".
            pass
        if browser.is_connected():
            try:
                await browser.close()
            except PlaywrightError:
                pass

    if video is None:
        raise RuntimeError("Playwright did not create a video object.")
    raw_path = Path(await video.path())
    shutil.move(str(raw_path), str(webm_path))

    if not successful or ready_at is None or game_started_at is None:
        webm_path.unlink(missing_ok=True)
        screenshot_path.unlink(missing_ok=True)
        print(f"Discarded {lifecycle.state.value} recording for room {room_id}.")
        if bridge is not None:
            await bridge.audit(
                room_id, "browser_recording_discarded", lifecycle.state.value
            )
        return False, replacement

    duration = max(0.1, stopped_at - game_started_at)
    conversion_task = asyncio.create_task(finalize_recording(
        webm_path,
        mp4_path,
        trim_start_seconds=max(0.0, game_started_at - capture_started_at),
        duration_seconds=duration,
        keep_webm=args.keep_webm,
        bridge=bridge,
        room_id=room_id,
    ))
    conversion_tasks.add(conversion_task)
    conversion_task.add_done_callback(conversion_tasks.discard)
    print(
        f"Queued background MP4 conversion for room {room_id}; "
        "resuming lobby detection immediately."
    )
    if bridge is not None:
        await bridge.audit(room_id, "browser_conversion_queued")
    return True, replacement


async def run(args) -> None:
    bridge = None
    conversion_tasks: set[asyncio.Task] = set()
    if args.bridge_url:
        bridge = PbotBridgeClient(
            args.bridge_url,
            args.bridge_secret,
            args.guild_id,
            args.recorder_id,
        )
        await bridge.start()
        print("Pbot replacement-lobby bridge enabled.")
    else:
        print("Pbot bridge is not configured; overlap handoff is disabled.")
    try:
      async with async_playwright() as playwright:
        preferred_room_id = None
        retry_delay = 5
        while True:
            # The lobby watcher owns the account connection only until a room
            # is selected. ProAvalon then transfers the account to the browser
            # socket, which becomes the sole lifecycle monitor.
            watcher = None
            completed = False
            replacement = None
            try:
                if bridge is not None:
                    dynamic_prefix = await bridge.active_season_prefix()
                    if dynamic_prefix is None:
                        raise RuntimeError(
                            "Pbot has no active season prefix configured."
                        )
                    elif dynamic_prefix.casefold() != args.season_prefix.casefold():
                        args.season_prefix = dynamic_prefix
                        print(
                            "Following Pbot's active season prefix: "
                            f"{args.season_prefix}"
                        )
                watcher = LobbyWatcher(args.username, args.password)
                await watcher.start()
                cookies = watcher.browser_cookies()
                (
                    room_id,
                    roster,
                    break_token,
                    args.season_prefix,
                ) = await watcher.wait_for_qualifying_room(
                    args.season_prefix,
                    preferred_room_id=preferred_room_id,
                    prefix_provider=(
                        bridge.active_season_prefix
                        if bridge is not None
                        else None
                    ),
                    room_claim=(bridge.claim_room if bridge is not None else None),
                    room_release=(bridge.release_room if bridge is not None else None),
                    claim_delay_seconds=args.claim_delay_seconds,
                )
                completed, replacement = await record_selected_room(
                    playwright,
                    watcher,
                    args,
                    room_id,
                    break_token,
                    cookies,
                    roster,
                    bridge,
                    conversion_tasks,
                )
                retry_delay = 5
            except asyncio.CancelledError:
                raise
            except Exception as error:
                print(
                    f"Recorder connection/session error: {error}. "
                    f"Re-authenticating in {retry_delay} seconds."
                )
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 60)
            finally:
                if watcher is not None:
                    await watcher.close()
                if bridge is not None and 'room_id' in locals():
                    try:
                        await bridge.release_room(room_id)
                    except Exception as error:
                        print(f"Could not release room {room_id} claim: {error}")
                    del room_id

            if completed:
                preferred_room_id = (
                    replacement["room_id"] if replacement is not None else None
                )
                if preferred_room_id is None:
                    print("Completed game captured. Waiting for the next valid lobby.")
                else:
                    print(
                        f"Completed game captured. Prioritizing replacement room "
                        f"{preferred_room_id}."
                    )
            else:
                preferred_room_id = (
                    replacement["room_id"] if replacement is not None else None
                )
                print("Returning to the lobby watcher for the next valid attempt.")
    finally:
        if conversion_tasks:
            print(
                f"Waiting for {len(conversion_tasks)} background MP4 "
                "conversion(s) to finish..."
            )
            await asyncio.gather(*conversion_tasks, return_exceptions=True)
        if bridge is not None:
            await bridge.close()


if __name__ == "__main__":
    try:
        asyncio.run(run(parse_args()))
    except KeyboardInterrupt:
        print("\nAutomatic recorder stopped.")
    except Exception as error:
        print(f"Automatic recorder error: {error}")
        raise SystemExit(1)
