from __future__ import annotations

import asyncio
import json
import os
import shutil
import ssl
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import aiohttp
import certifi
from dotenv import load_dotenv


@dataclass
class ActiveRecording:
    room_id: int
    players: list[str]
    channel_id: int
    recording_id: str
    access_key: str
    invalid_since: float | None = None
    postgame_deadline: float | None = None


class Controller:
    def __init__(self) -> None:
        load_dotenv()
        self.bridge_url = os.environ["RECORDER_BRIDGE_URL"].rstrip("/")
        self.bridge_secret = os.environ["RECORDER_BRIDGE_SECRET"]
        self.guild_id = int(os.environ["DISCORD_GUILD_ID"])
        self.recorder_id = os.getenv("RECORDER_INSTANCE_ID", "recorder-1").strip()
        self.season_prefix = os.getenv("RECORDER_SEASON_PREFIX", "").strip().casefold()
        self.reported_season_prefix: str | None = None
        self.control_url = os.getenv("CRAIG_CONTROL_URL", "http://127.0.0.1:9300").rstrip("/")
        self.control_token = os.environ["CRAIG_CONTROL_TOKEN"]
        self.ferret_url = os.getenv("CRAIG_DOWNLOAD_URL", "http://127.0.0.1:9100").rstrip("/")
        self.craig_data_dir = Path(os.getenv("CRAIG_DATA_DIR", "../craig-data")).resolve()
        self.output_dir = Path(os.getenv("AUDIO_OUTPUT_DIR", "recordings")).resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.video_dir = Path(os.getenv("VIDEO_OUTPUT_DIR", "../browser-recordings")).resolve()
        self.final_dir = Path(os.getenv("FINAL_OUTPUT_DIR", "final-recordings")).resolve()
        self.final_dir.mkdir(parents=True, exist_ok=True)
        self.session: aiohttp.ClientSession | None = None
        self.active: ActiveRecording | None = None
        self.processing_handoffs: set[Path] = set()
        self.orphan_retention_seconds = max(
            3600,
            int(os.getenv("RECORDER_ORPHAN_RETENTION_HOURS", "24")) * 3600,
        )
        self.next_cleanup_at = 0.0
        self.ssl_context = ssl.create_default_context(cafile=certifi.where())

    async def request(self, method: str, url: str, *, bridge=False, **kwargs):
        headers = kwargs.pop("headers", {})
        secret = self.bridge_secret if bridge else self.control_token
        headers["Authorization"] = f"Bearer {secret}"
        assert self.session is not None
        async with self.session.request(method, url, headers=headers, **kwargs) as response:
            try:
                data = await response.json(content_type=None)
            except (aiohttp.ContentTypeError, json.JSONDecodeError):
                data = {"error": await response.text()}
            if response.status >= 400:
                detail = data.get("error") or data.get("code")
                raise RuntimeError(
                    f"HTTP {response.status}"
                    + (f": {detail}" if detail is not None else "")
                )
            return data

    async def snapshot(self):
        return await self.request(
            "GET",
            f"{self.bridge_url}/recorder/lobbies?guild_id={self.guild_id}",
            bridge=True,
        )

    def candidate(self, payload, *, exclude_room=None):
        active_season = payload.get("active_season") or {}
        season_prefix = str(
            (active_season.get("required_prefix") or "")
            if "active_season" in payload
            else self.season_prefix
        ).strip().casefold()
        if not season_prefix:
            return None
        if season_prefix != self.reported_season_prefix:
            print(f"Following Pbot's active season prefix: {season_prefix}")
            self.reported_season_prefix = season_prefix
        links = payload.get("account_links") or {}
        voice_channels = payload.get("voice_channels") or []
        claimed_rooms = {
            int(claim["room_id"])
            for claim in payload.get("room_claims") or []
            if str(claim.get("recorder_id") or "") == self.recorder_id
        }
        candidates = []
        for lobby in payload.get("lobbies") or []:
            try:
                room_id = int(lobby["room_id"])
                players = [str(name) for name in lobby.get("players") or []]
                status = str(lobby.get("status") or "").casefold()
            except (KeyError, TypeError, ValueError):
                continue
            if room_id == exclude_room or status != "game in progress" or len(players) != 10:
                continue
            if room_id not in claimed_rooms:
                continue
            if not all(name.casefold().startswith(season_prefix) for name in players):
                continue
            linked_ids = {int(links[name.casefold()]) for name in players if name.casefold() in links}
            for channel in voice_channels:
                overlap = linked_ids.intersection(int(value) for value in channel.get("member_ids") or [])
                if len(overlap) >= 6:
                    candidates.append((len(overlap), room_id, players, int(channel["channel_id"])))
        return max(candidates, default=None)

    async def start(self, candidate, payload):
        _, room_id, players, channel_id = candidate
        config = payload.get("recording_config") or {}
        body = {
            "guildId": str(self.guild_id),
            "channelId": str(channel_id),
        }
        if config.get("output_channel_id"):
            body["postChannelId"] = str(config["output_channel_id"])
        result = await self.request("POST", f"{self.control_url}/recordings/start", json=body)
        if not result.get("id") or not result.get("accessKey"):
            raise RuntimeError(
                "Craig started without returning recording metadata; redeploy "
                "the Craig async control-response fix."
            )
        self.active = ActiveRecording(
            room_id=room_id,
            players=players,
            channel_id=channel_id,
            recording_id=result["id"],
            access_key=result["accessKey"],
        )
        print(f"Recording room {room_id} in Discord voice channel {channel_id}.")

    async def stop(self, *, discard: bool):
        assert self.active is not None
        current = self.active
        result = await self.request(
            "POST",
            f"{self.control_url}/recordings/stop",
            json={"guildId": str(self.guild_id), "discard": discard},
        )
        self.active = None
        if discard:
            print(f"Discarded abandoned room {current.room_id}.")
            return
        asyncio.create_task(self._prepare_audio_handoff(current, result))
        print(f"Room {current.room_id} stopped; audio processing continues in the background.")

    async def _prepare_audio_handoff(self, current: ActiveRecording, result: dict):
        try:
            mixed_path = await self.create_mixed_mp3(
                result["id"], result["accessKey"], current.room_id
            )
        except Exception as error:
            print(f"Craig mix for room {current.room_id} failed: {error}")
            return
        metadata = {
            "room_id": current.room_id,
            "discord_voice_channel_id": current.channel_id,
            "recording_id": result["id"],
            "access_key": result["accessKey"],
            "completed_at": time.time(),
            "mixed_mp3": str(mixed_path),
        }
        path = self.output_dir / f"room-{current.room_id}-craig.json"
        path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        print(f"Completed room {current.room_id}; saved Craig handoff metadata to {path}.")

    def matching_video(self, room_id: int) -> Path | None:
        if not self.video_dir.exists():
            return None
        matches = [
            path
            for path in self.video_dir.rglob(f"proavalon-room-{room_id}-*.mp4")
            if ".converting." not in path.name and path.stat().st_size > 0
        ]
        return max(matches, key=lambda path: path.stat().st_mtime, default=None)

    def combine_recording(self, video_path: Path, audio_path: Path, room_id: int) -> Path:
        destination = self.final_dir / f"proavalon-room-{room_id}-complete.mp4"
        if destination.is_file() and destination.stat().st_size > 0:
            return destination
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("FFmpeg is not installed or is not available in PATH")
        subprocess.run(
            [
                ffmpeg, "-y", "-i", str(video_path), "-i", str(audio_path),
                "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k", "-shortest", str(destination),
            ],
            check=True,
        )
        return destination

    async def upload_to_youtube(self, final_path: Path, room_id: int) -> str:
        start = await self.request(
            "POST",
            f"{self.bridge_url}/recorder/youtube/uploads/start",
            bridge=True,
            json={
                "guild_id": self.guild_id,
                "room_id": room_id,
                "file_size": final_path.stat().st_size,
            },
        )
        if start.get("already_complete"):
            return str(start["youtube_url"])
        upload_url = str(start["upload_url"])
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None),
            connector=aiohttp.TCPConnector(ssl=self.ssl_context),
        ) as upload_session:
            with final_path.open("rb") as video:
                async with upload_session.put(
                    upload_url,
                    data=video,
                    headers={
                        "Content-Type": "video/mp4",
                        "Content-Length": str(final_path.stat().st_size),
                    },
                ) as response:
                    result = await response.json(content_type=None)
                    if response.status not in {200, 201} or not result.get("id"):
                        raise RuntimeError(
                            result.get("error", {}).get("message")
                            or f"YouTube upload failed with HTTP {response.status}"
                        )
        complete = await self.request(
            "POST",
            f"{self.bridge_url}/recorder/youtube/uploads/complete",
            bridge=True,
            json={
                "guild_id": self.guild_id,
                "room_id": room_id,
                "video_id": result["id"],
            },
        )
        return str(complete["youtube_url"])

    async def _process_handoff(self, metadata_path: Path) -> None:
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if metadata.get("youtube_url"):
                if metadata.get("cleanup_after_upload"):
                    self.cleanup_completed_recording(metadata_path, metadata)
                return
            room_id = int(metadata["room_id"])
            video_path = self.matching_video(room_id)
            audio_path = Path(metadata["mixed_mp3"])
            if video_path is None or not audio_path.is_file():
                return
            final_path = await asyncio.to_thread(
                self.combine_recording, video_path, audio_path, room_id
            )
            youtube_url = await self.upload_to_youtube(final_path, room_id)
            metadata["final_mp4"] = str(final_path)
            metadata["youtube_url"] = youtube_url
            metadata["cleanup_after_upload"] = True
            metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
            print(f"Uploaded room {room_id} to YouTube: {youtube_url}")
            self.cleanup_completed_recording(metadata_path, metadata)
        except Exception as error:
            print(f"Room recording handoff will retry: {error}")
        finally:
            self.processing_handoffs.discard(metadata_path)

    def _managed_media_roots(self) -> tuple[Path, ...]:
        return (
            self.video_dir.resolve(),
            self.output_dir.resolve(),
            self.final_dir.resolve(),
        )

    def _safe_unlink(self, path: Path) -> int:
        try:
            resolved = path.resolve()
            if not any(resolved.is_relative_to(root) for root in self._managed_media_roots()):
                return 0
            if not resolved.is_file():
                return 0
            size = resolved.stat().st_size
            resolved.unlink()
            return size
        except OSError:
            return 0

    def cleanup_completed_recording(self, metadata_path: Path, metadata: dict) -> None:
        try:
            room_id = int(metadata["room_id"])
        except (KeyError, TypeError, ValueError):
            return
        paths = {
            Path(str(metadata.get("mixed_mp3") or "")),
            Path(str(metadata.get("final_mp4") or "")),
            self.final_dir / f"proavalon-room-{room_id}-complete.mp4",
        }
        if self.video_dir.exists():
            paths.update(self.video_dir.rglob(f"proavalon-room-{room_id}-*"))
        freed = sum(self._safe_unlink(path) for path in paths if str(path))
        freed += self._safe_unlink(metadata_path)
        print(
            f"Cleaned uploaded room {room_id} media "
            f"({freed / (1024 * 1024):.1f} MiB freed)."
        )

    def cleanup_orphaned_media(self) -> None:
        protected: set[Path] = set()
        for metadata_path in self.output_dir.glob("room-*-craig.json"):
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if metadata.get("youtube_url") and metadata.get("cleanup_after_upload"):
                self.cleanup_completed_recording(metadata_path, metadata)
                continue
            protected.add(metadata_path.resolve())
            for key in ("mixed_mp3", "final_mp4"):
                value = str(metadata.get(key) or "")
                if value:
                    protected.add(Path(value).resolve())
            try:
                room_id = int(metadata["room_id"])
            except (KeyError, TypeError, ValueError):
                continue
            if self.video_dir.exists():
                protected.update(
                    path.resolve()
                    for path in self.video_dir.rglob(f"proavalon-room-{room_id}-*")
                )

        cutoff = time.time() - self.orphan_retention_seconds
        candidates: list[Path] = []
        if self.video_dir.exists():
            candidates.extend(path for path in self.video_dir.rglob("*") if path.is_file())
        candidates.extend(self.output_dir.glob("proavalon-room-*-audio.mp3"))
        candidates.extend(self.final_dir.glob("proavalon-room-*-complete.mp4"))
        freed = 0
        removed = 0
        for path in candidates:
            try:
                if path.resolve() in protected or path.stat().st_mtime >= cutoff:
                    continue
            except OSError:
                continue
            size = self._safe_unlink(path)
            if size:
                freed += size
                removed += 1
        if removed:
            print(
                f"Cleaned {removed} orphaned recorder files older than "
                f"{self.orphan_retention_seconds // 3600}h "
                f"({freed / (1024 * 1024):.1f} MiB freed)."
            )

    def process_handoffs(self) -> None:
        for metadata_path in sorted(self.output_dir.glob("room-*-craig.json")):
            if metadata_path in self.processing_handoffs:
                continue
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if metadata.get("youtube_url"):
                if metadata.get("cleanup_after_upload"):
                    self.cleanup_completed_recording(metadata_path, metadata)
                continue
            self.processing_handoffs.add(metadata_path)
            asyncio.create_task(self._process_handoff(metadata_path))

    async def create_mixed_mp3(self, recording_id: str, access_key: str, room_id: int) -> Path:
        job_url = f"{self.ferret_url}/api/v1/recordings/{recording_id}/job?key={access_key}"
        await self.request(
            "POST",
            job_url,
            json={"type": "recording", "options": {"container": "mix", "format": "mp3"}},
            headers={},
        )
        deadline = time.monotonic() + 15 * 60
        while time.monotonic() < deadline:
            assert self.session is not None
            async with self.session.get(job_url) as response:
                payload = await response.json(content_type=None)
            if response.status >= 400:
                raise RuntimeError(payload.get("error") or "Could not read Craig processing job")
            job = payload.get("job") or {}
            if job.get("status") == "error":
                raise RuntimeError("Craig failed to create the mixed MP3")
            if job.get("status") == "complete" and job.get("outputFileName"):
                source = self.craig_data_dir / "downloads" / job["outputFileName"]
                for _ in range(20):
                    if source.is_file():
                        destination = self.output_dir / f"proavalon-room-{room_id}-audio.mp3"
                        shutil.copy2(source, destination)
                        print(f"Mixed MP3 ready: {destination}")
                        return destination
                    await asyncio.sleep(0.5)
                destination = self.output_dir / f"proavalon-room-{room_id}-audio.mp3"
                download_url = (
                    f"{self.ferret_url}/dl/{quote(str(job['outputFileName']))}"
                )
                assert self.session is not None
                async with self.session.get(download_url) as download:
                    if download.status >= 400:
                        raise RuntimeError(
                            "Craig completed the mix but its audio download was unavailable"
                        )
                    with destination.open("wb") as output:
                        async for chunk in download.content.iter_chunked(1024 * 1024):
                            output.write(chunk)
                print(f"Mixed MP3 downloaded: {destination}")
                return destination
            await asyncio.sleep(1)
        raise TimeoutError("Craig did not finish the mixed MP3 within 15 minutes")

    async def tick(self):
        payload = await self.snapshot()
        self.process_handoffs()
        now = time.monotonic()
        if now >= self.next_cleanup_at:
            self.next_cleanup_at = now + 600
            self.cleanup_orphaned_media()
        config = payload.get("recording_config") or {}
        if self.active is None:
            if config.get("enabled"):
                candidate = self.candidate(payload)
                if candidate:
                    await self.start(candidate, payload)
            return

        current = self.active
        replacement = self.candidate(payload, exclude_room=current.room_id)
        if replacement:
            returning = len({p.casefold() for p in current.players}.intersection(p.casefold() for p in replacement[2]))
            if returning >= 3:
                await self.stop(discard=current.postgame_deadline is None)
                await self.start(replacement, payload)
                return

        lobby = next((item for item in payload.get("lobbies") or [] if int(item.get("room_id", -1)) == current.room_id), None)
        now = time.monotonic()
        status = str((lobby or {}).get("status") or "").casefold()
        if status == "finished" and current.postgame_deadline is None:
            current.postgame_deadline = now + 30
            current.invalid_since = None
            print(f"Room {current.room_id} finished; recording 30 seconds postgame.")
        if current.postgame_deadline is not None:
            if now >= current.postgame_deadline:
                await self.stop(discard=False)
            return

        try:
            count = int(lobby.get("player_count")) if lobby else None
        except (TypeError, ValueError):
            count = None
        invalid = lobby is None or count is None or count <= 4
        if invalid and current.invalid_since is None:
            current.invalid_since = now
        elif not invalid:
            current.invalid_since = None
        elif now - current.invalid_since >= 120:
            await self.stop(discard=True)

    async def run(self):
        timeout = aiohttp.ClientTimeout(total=15)
        connector = aiohttp.TCPConnector(ssl=self.ssl_context)
        async with aiohttp.ClientSession(
            timeout=timeout, connector=connector
        ) as session:
            self.session = session
            print("Automatic ProAvalon audio controller is running.")
            while True:
                try:
                    await self.tick()
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    print(f"Controller check failed temporarily: {error}")
                await asyncio.sleep(1)


if __name__ == "__main__":
    try:
        asyncio.run(Controller().run())
    except KeyboardInterrupt:
        print("Automatic audio controller stopped.")
