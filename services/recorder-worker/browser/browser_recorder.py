from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import BrowserContext, Page, TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright


PROAVALON_BASE_URL = "https://proavalon.com"
DEFAULT_VIEWPORT = {"width": 1920, "height": 1080}


@dataclass(frozen=True)
class RecorderConfig:
    username: str
    password: str
    room_id: int
    duration_seconds: int
    output_dir: Path
    headless: bool
    keep_webm: bool


def parse_args() -> RecorderConfig:
    parser = argparse.ArgumentParser(
        description=(
            "Record the actual ProAvalon spectator webpage using the dedicated "
            "recorder account. The visual layout is forced to dark mode with "
            "Game Chat on the left and Vote History on the right."
        )
    )
    parser.add_argument("room_id", type=int, help="ProAvalon room ID to spectate")
    parser.add_argument(
        "--duration",
        type=int,
        default=60,
        help="Test recording length in seconds (default: 60)",
    )
    parser.add_argument(
        "--output-dir",
        default="recordings",
        help="Directory where the test recording is written",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Show Chromium while testing instead of running headless",
    )
    parser.add_argument(
        "--keep-webm",
        action="store_true",
        help="Keep Playwright's original WebM after MP4 conversion",
    )
    args = parser.parse_args()

    load_dotenv()
    username = os.getenv("RECORDER_PROAVALON_USERNAME", "").strip()
    password = os.getenv("RECORDER_PROAVALON_PASSWORD", "")

    if not username or not password:
        parser.error(
            "Set RECORDER_PROAVALON_USERNAME and RECORDER_PROAVALON_PASSWORD "
            "in .env before starting the recorder."
        )

    if args.room_id <= 0:
        parser.error("room_id must be greater than zero")
    if args.duration <= 0:
        parser.error("--duration must be greater than zero")

    return RecorderConfig(
        username=username,
        password=password,
        room_id=args.room_id,
        duration_seconds=args.duration,
        output_dir=Path(args.output_dir).resolve(),
        headless=not args.headed,
        keep_webm=args.keep_webm,
    )


async def login(page: Page, username: str, password: str) -> None:
    print("Opening ProAvalon...")
    await page.goto(PROAVALON_BASE_URL, wait_until="domcontentloaded")

    # Already-authenticated persistent browser state can land directly in the lobby.
    if await page.locator("#current-games-table").count() > 0:
        print("Recorder browser is already logged in.")
        return

    print("Logging into the dedicated ProAvalon recorder account...")
    await page.locator("#username").fill(username)
    await page.locator('input[name="password"]').fill(password)

    # Submit the site's real login form.
    await page.locator("#form").evaluate("form => form.submit()")

    try:
        await page.wait_for_selector("#current-games-table", timeout=20_000)
    except PlaywrightTimeoutError as error:
        raise RuntimeError(
            "ProAvalon login did not reach the lobby. Check the recorder account "
            "credentials and whether ProAvalon presented an additional login step."
        ) from error

    print("ProAvalon login successful.")


async def force_recorder_preferences(context: BrowserContext, page: Page) -> None:
    """Force the two persistent ProAvalon display options used by the VOD layout."""
    print("Applying recorder visual preferences: dark mode + two tabs...")

    await context.add_cookies(
        [
            {
                "name": "optionDisplayDarkTheme",
                "value": "true",
                "url": PROAVALON_BASE_URL,
            },
            {
                "name": "optionDisplayTwoTabs",
                "value": "true",
                "url": PROAVALON_BASE_URL,
            },
            {
                # ProAvalon emits the Welcome! SweetAlert whenever this cookie
                # is absent. Recorder contexts are intentionally fresh on every
                # run, so set it every time before reloading/joining a room.
                "name": "seenNewPlayerIntro",
                "value": "true",
                "url": PROAVALON_BASE_URL,
            },
        ]
    )

    # The site applies these preferences from cookies on page load.
    await page.reload(wait_until="domcontentloaded")
    await page.wait_for_selector("#current-games-table", timeout=20_000)


async def dismiss_known_popups(page: Page) -> None:
    """Dismiss harmless ProAvalon informational popups before recording."""
    # Defense in depth: the seenNewPlayerIntro cookie should prevent the
    # Welcome! modal, but if the site's client has already queued it, close it.
    for _ in range(4):
        popup = page.locator('.swal2-popup:visible, .sweet-alert:visible').first
        if await popup.count() == 0:
            await asyncio.sleep(0.25)
            continue

        title = ''
        try:
            title = (await popup.locator('.swal2-title, h2').first.inner_text()).strip()
        except Exception:
            pass

        if title.casefold() == 'welcome!':
            ok = popup.get_by_role('button', name='OK', exact=True)
            if await ok.count() == 0:
                ok = popup.locator('.swal2-confirm, button.confirm').first
            if await ok.count() > 0:
                print('Dismissing ProAvalon Welcome popup...')
                await ok.click()
                await asyncio.sleep(0.25)
                continue
        break


async def join_room_as_spectator(page: Page, room_id: int) -> None:
    print(f"Joining ProAvalon room {room_id} as a spectator...")

    # ProAvalon's own lobby client exposes joinRoom(roomId), which performs the
    # normal spectator join path and switches to the in-room display.
    try:
        await page.wait_for_function("typeof window.joinRoom === 'function'", timeout=15_000)
        await page.evaluate("roomId => window.joinRoom(roomId)", room_id)
    except PlaywrightTimeoutError as error:
        raise RuntimeError(
            "The ProAvalon lobby did not expose its normal joinRoom function. "
            "The site's client layout may have changed."
        ) from error

    # Wait for the room view to be visible. #backButton belongs to the in-room view.
    try:
        await page.locator("#backButton").wait_for(state="visible", timeout=15_000)
    except PlaywrightTimeoutError as error:
        raise RuntimeError(
            f"Room {room_id} did not become visible. It may no longer exist, may "
            "be password protected, or the recorder may have been unable to spectate it."
        ) from error


async def lock_recording_tabs(page: Page) -> None:
    """Select exactly Game Chat (left) + Vote History (right)."""
    print("Locking left pane to Game Chat and right pane to Vote History...")

    left_game_chat = page.locator('#tabs1 a[href="#room-chat-in-game"]')
    right_vote_history = page.locator('#tabs2 a[href="#vh2"]')

    await left_game_chat.wait_for(state="visible", timeout=10_000)
    await right_vote_history.wait_for(state="visible", timeout=10_000)

    await left_game_chat.click()
    await right_vote_history.click()

    # Verify the two corresponding content panes became active.
    await page.wait_for_function(
        """
        () => {
          const left = document.querySelector('#room-chat-in-game');
          const right = document.querySelector('#vh2');
          return left?.classList.contains('active') && right?.classList.contains('active');
        }
        """,
        timeout=10_000,
    )


async def verify_layout(page: Page) -> None:
    checks = await page.evaluate(
        """
        () => ({
          darkChecked: !!document.querySelector('#option_display_dark_theme')?.checked,
          twoTabsChecked: !!document.querySelector('#option_display_two_tabs')?.checked,
          tabs2Visible: (() => {
            const el = document.querySelector('#tabs2');
            if (!el) return false;
            const style = window.getComputedStyle(el);
            return style.display !== 'none' && !el.classList.contains('displayNoneClass');
          })(),
          leftGameChatActive: !!document.querySelector('#room-chat-in-game')?.classList.contains('active'),
          rightVoteHistoryActive: !!document.querySelector('#vh2')?.classList.contains('active'),
        })
        """
    )

    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise RuntimeError(
            "Recorder layout verification failed: " + ", ".join(failed)
        )

    print("Layout verified: dark mode, split screen, Game Chat + Vote History.")


def convert_webm_to_mp4(
    webm_path: Path,
    mp4_path: Path,
    *,
    trim_start_seconds: float,
    duration_seconds: float,
) -> bool:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        print("FFmpeg was not found. Keeping the WebM test recording instead.")
        return False

    print("Converting browser recording to MP4 with FFmpeg...")
    command = [
        ffmpeg,
        "-y",
        "-i",
        str(webm_path),
        # Playwright records from page creation and cannot begin video capture
        # on demand. Remove login, lobby, popup, and layout setup from the
        # deliverable using the monotonic ready marker captured below.
        "-ss",
        f"{trim_start_seconds:.3f}",
        "-t",
        str(duration_seconds),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(mp4_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        print("FFmpeg conversion failed. The original WebM is still available.")
        print(result.stderr[-2000:])
        return False
    return True


async def run(config: RecorderConfig) -> None:
    config.output_dir.mkdir(parents=True, exist_ok=True)
    raw_video_dir = config.output_dir / "_playwright_video"
    raw_video_dir.mkdir(parents=True, exist_ok=True)

    timestamp = time.strftime("%Y%m%d-%H%M%S")
    final_base = f"proavalon-room-{config.room_id}-{timestamp}"
    screenshot_path = config.output_dir / f"{final_base}-layout-check.png"
    mp4_path = config.output_dir / f"{final_base}.mp4"
    webm_path = config.output_dir / f"{final_base}.webm"

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=config.headless,
            args=[
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )

        context = await browser.new_context(
            viewport=DEFAULT_VIEWPORT,
            record_video_dir=str(raw_video_dir),
            record_video_size=DEFAULT_VIEWPORT,
        )
        # Playwright starts its context video when the page is created. Keep a
        # monotonic anchor so the final encode can discard everything before
        # the verified in-room layout is ready.
        capture_started_at = time.monotonic()
        page = await context.new_page()
        video = page.video

        try:
            await login(page, config.username, config.password)
            await force_recorder_preferences(context, page)
            await join_room_as_spectator(page, config.room_id)
            await dismiss_known_popups(page)
            await lock_recording_tabs(page)
            await verify_layout(page)

            await page.screenshot(path=str(screenshot_path), full_page=False)
            print(f"Saved layout verification screenshot: {screenshot_path}")

            recording_started_at = time.monotonic()
            trim_start_seconds = max(
                0.0,
                recording_started_at - capture_started_at,
            )

            print(
                f"Recording the actual ProAvalon webpage for "
                f"{config.duration_seconds} seconds..."
            )
            await asyncio.sleep(config.duration_seconds)
            print("Test recording complete.")
        finally:
            # Closing the context finalizes Playwright's video file.
            await context.close()
            await browser.close()

        if video is None:
            raise RuntimeError("Playwright did not create a page video object.")

        raw_path = Path(await video.path())
        shutil.move(str(raw_path), str(webm_path))

    converted = convert_webm_to_mp4(
        webm_path,
        mp4_path,
        trim_start_seconds=trim_start_seconds,
        duration_seconds=config.duration_seconds,
    )
    if converted:
        print(
            f"MP4 ready: {mp4_path} "
            "(login and browser setup removed)"
        )
        if not config.keep_webm:
            webm_path.unlink(missing_ok=True)
    else:
        print(
            f"Raw WebM ready: {webm_path}\n"
            "WARNING: Playwright's raw WebM includes browser setup. Install "
            "FFmpeg and rerun before sharing the recording."
        )

    # Remove Playwright's now-empty scratch directory when possible.
    try:
        raw_video_dir.rmdir()
    except OSError:
        pass


if __name__ == "__main__":
    try:
        asyncio.run(run(parse_args()))
    except KeyboardInterrupt:
        print("\nRecorder stopped.")
        sys.exit(130)
    except Exception as error:
        print(f"Recorder error: {error}", file=sys.stderr)
        sys.exit(1)
