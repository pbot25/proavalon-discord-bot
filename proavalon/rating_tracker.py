import asyncio
import hashlib
import json
from datetime import datetime, timezone
from io import BytesIO

import aiohttp
import discord

from database.matches import (
    get_enabled_match_configs,
    mark_match_posted,
    save_match,
)
from database.ratings import (
    get_cached_rating,
    load_rating_cache,
    upsert_online_ratings,
)
from database.discord_roles import get_active_season_linked_users
from database.seasons import (
    get_all_active_seasons,
    register_season_player,
)
from proavalon.vote_chart import build_vote_history_png


class ProAvalonRatingTracker:
    """One authenticated socket for ratings plus automatic season matches."""

    SOCKET_URL = "wss://proavalon.com/socket.io/?EIO=4&transport=websocket"

    def __init__(self, scraper, bot=None) -> None:
        self.scraper = scraper
        self.bot = bot
        self._task = None
        self._stop_event = asyncio.Event()
        self._websocket = None
        self._games = {}
        self._room_rosters = {}
        self._tracked = {}  # room -> state
        self._current_room = None
        self._last_switch = 0.0
        self._finalizing = set()
        # Rooms are inspected only after a game starts, when the 10-player
        # roster is locked. This prevents repeated join/leave spam.
        self._initially_inspected = set()
        self._final_visit_started = set()

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_forever(), name="proavalon-live-tracker")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run_forever(self) -> None:
        cached = await load_rating_cache()
        print(f"Loaded {cached} persisted ProAvalon ratings.")
        delay = 5
        while not self._stop_event.is_set():
            try:
                await self._listen_once()
                delay = 5
            except asyncio.CancelledError:
                raise
            except Exception as error:
                print(f"ProAvalon live socket disconnected: {type(error).__name__}: {error}")
            if self._stop_event.is_set():
                break
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    async def _emit(self, event, *args):
        if self._websocket is None:
            return
        await self._websocket.send_str("42" + json.dumps([event, *args], separators=(",", ":")))

    async def _room_rotator(self):
        """
        Inspect each 10-player game at most twice:

        1. Once after the game has actually started, to lock in the roster
           and pre-game ratings.
        2. Once after the global lobby reports Finished, to receive the
           cumulative final game-data (votes, roles, missions, shot, etc.).

        ProAvalon vote history is cumulative, so there is no reason to keep
        bouncing in and out of rooms during the match.
        """
        while (
            not self._stop_event.is_set()
            and self._websocket is not None
        ):
            existing_room_ids = set(self._games)

            # Forget IDs after rooms disappear. This also makes the tracker
            # safe if ProAvalon eventually reuses a room ID.
            self._initially_inspected.intersection_update(existing_room_ids)
            self._final_visit_started.intersection_update(existing_room_ids)

            # Final snapshots are the priority because finished rooms can be
            # destroyed once everybody leaves.
            final_room = None
            for room_id in list(self._tracked):
                game = self._games.get(room_id)
                if not game:
                    continue
                status = str(game.get("status") or "").casefold()
                if (
                    status == "finished"
                    and room_id not in self._final_visit_started
                    and room_id not in self._finalizing
                ):
                    final_room = room_id
                    break

            if final_room is not None:
                self._final_visit_started.add(final_room)
                self._current_room = final_room
                await self._emit("join-room", final_room)
                # distributeGameData() is called immediately by ProAvalon
                # after a successful join-room. Leave enough time for the
                # room roster and final game-data events to arrive.
                await asyncio.sleep(1.25)
                await self._emit("leave-room", "")
                self._current_room = None
                await asyncio.sleep(0.35)
                continue

            # Only inspect a candidate after the game starts. At that point
            # playersInGame is fixed, so a non-season room never needs to be
            # inspected again during that game.
            initial_room = None
            for room_id, game in self._games.items():
                if room_id in self._initially_inspected:
                    continue

                status = str(game.get("status") or "").casefold()
                if status != "game in progress":
                    continue

                try:
                    count = int(game.get("numOfPlayersInside", 0))
                    maximum = int(game.get("maxNumPlayers", 0))
                except (TypeError, ValueError):
                    continue

                if (
                    count == 10
                    and maximum == 10
                    and not game.get("passwordLocked", False)
                ):
                    initial_room = room_id
                    break

            if initial_room is not None:
                # Mark it before joining so a transient event or exception
                # does not create a rapid join/leave loop.
                self._initially_inspected.add(initial_room)
                self._current_room = initial_room
                await self._emit("join-room", initial_room)
                await asyncio.sleep(1.25)
                await self._emit("leave-room", "")
                self._current_room = None
                await asyncio.sleep(0.35)
                continue

            await asyncio.sleep(0.75)

    async def _listen_once(self) -> None:
        source = self.scraper.get_authenticated_session()
        jar = aiohttp.CookieJar()
        for cookie in source.cookie_jar:
            jar.update_cookies({cookie.key: cookie.value})
        timeout = aiohttp.ClientTimeout(total=None, connect=20, sock_connect=20, sock_read=None)
        async with aiohttp.ClientSession(timeout=timeout, cookie_jar=jar, headers={"User-Agent":"Mozilla/5.0 ProAvalon-Community-Stats-Bot","Origin":"https://proavalon.com"}) as session:
            async with session.ws_connect(self.SOCKET_URL, heartbeat=None, autoping=True) as ws:
                self._websocket = ws
                rotator = None
                connected = False
                try:
                    async for message in ws:
                        if message.type != aiohttp.WSMsgType.TEXT:
                            if message.type in {aiohttp.WSMsgType.CLOSED,aiohttp.WSMsgType.CLOSE,aiohttp.WSMsgType.ERROR}:
                                break
                            continue
                        data = message.data
                        if data.startswith("0"):
                            await ws.send_str("40"); continue
                        if data == "2":
                            await ws.send_str("3"); continue
                        if data.startswith("40"):
                            if not connected:
                                connected = True
                                print("ProAvalon live rating/match socket connected.")
                                rotator = asyncio.create_task(self._room_rotator())
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
                        if event == "update-current-players-list" and isinstance(event_data, list):
                            count = await upsert_online_ratings(event_data)
                            if count:
                                print(f"Updated last-known ratings for {count} online players.")
                        elif event == "update-current-games-list" and isinstance(event_data, list):
                            self._games = {int(g["roomId"]): g for g in event_data if isinstance(g, dict) and g.get("roomId") is not None}
                        elif event == "update-room-players" and isinstance(event_data, list) and self._current_room is not None:
                            names = [p.get("username") for p in event_data if isinstance(p, dict) and isinstance(p.get("username"), str)]
                            if names:
                                self._room_rosters[self._current_room] = names
                                await self._consider_roster(self._current_room, names)
                        elif event == "game-data" and isinstance(event_data, dict):
                            await self._handle_game_data(event_data)
                finally:
                    if rotator:
                        rotator.cancel()
                    self._websocket = None
                    self._current_room = None
        raise ConnectionError("ProAvalon closed the live socket.")

    async def _consider_roster(self, room_id, names):
        if len(names) != 10:
            return
        seasons = await get_all_active_seasons()
        matched = []
        for season in seasons:
            prefix = str(season["required_prefix"]).casefold()
            if all(name.casefold().startswith(prefix) for name in names):
                matched.append(season)
        if not matched:
            return
        state = self._tracked.setdefault(room_id, {"detected_at": datetime.now(timezone.utc).isoformat(), "seasons": {}, "pre": {}, "last_data": None})
        for name in names:
            snap = get_cached_rating(name)
            if snap is not None and name.casefold() not in state["pre"]:
                state["pre"][name.casefold()] = {"rating": snap.rating, "bracket": snap.rating_bracket}
        for season in matched:
            sid = int(season["id"])
            state["seasons"][sid] = dict(season)
            for name in names:
                await register_season_player(sid, name)  # void list is respected here
        print(f"Tracking qualifying season match in ProAvalon room {room_id}: {', '.join(names)}")

    async def _handle_game_data(self, data):
        room_id = data.get("roomId")
        if room_id is None:
            return
        try: room_id = int(room_id)
        except (TypeError, ValueError): return
        names = self._room_rosters.get(room_id)
        if not names:
            raw = data.get("playerUsernamesOrdered") or []
            if len(raw) == 10 and all(isinstance(x, str) for x in raw):
                names = raw
                await self._consider_roster(room_id, names)
        state = self._tracked.get(room_id)
        if state is None:
            return
        state["last_data"] = data
        if str(data.get("phase", "")).casefold() == "finished" and room_id not in self._finalizing:
            self._finalizing.add(room_id)
            asyncio.create_task(self._finalize_room(room_id))

    async def _finalize_room(self, room_id):
        try:
            # finishGame broadcasts refreshed ratings immediately afterward.
            await asyncio.sleep(2.5)
            state = self._tracked.get(room_id)
            if not state or not state.get("last_data"):
                return
            data = state["last_data"]
            real_players = self._room_rosters.get(room_id) or data.get("playerUsernamesOrdered") or []
            roles = ((data.get("see") or {}).get("roles") or [])
            changes = []
            for idx, name in enumerate(real_players):
                before = state["pre"].get(name.casefold())
                after = get_cached_rating(name)
                changes.append({
                    "username": name,
                    "role": roles[idx] if idx < len(roles) else None,
                    "before": before["rating"] if before else None,
                    "after": after.rating if after else None,
                    "change": (after.rating-before["rating"]) if before and after else None,
                    "old_bracket": before["bracket"] if before else None,
                    "new_bracket": after.rating_bracket if after else None,
                })
            canonical = json.dumps({"room":room_id,"players":[x.casefold() for x in real_players],"winner":data.get("winner"),"missions":data.get("missionHistory"),"votes":data.get("voteHistory")}, sort_keys=True, separators=(",",":"))
            fingerprint = hashlib.sha256(canonical.encode()).hexdigest()
            for sid, season in state["seasons"].items():
                inserted = await save_match(season_id=sid,fingerprint=fingerprint,room_id=room_id,detected_at=state["detected_at"],winner=data.get("winner"),game_mode=(self._games.get(room_id) or {}).get("gameMode"),mission_history=data.get("missionHistory") or [],fails_history=data.get("numFailsHistory") or [],vote_history=data.get("voteHistory"),players=real_players,roles=roles,rating_changes=changes)
                if inserted:
                    # Refresh Discord roles immediately after every newly archived
                    # qualifying match. The 15-minute task remains only a backup.
                    role_sync_result = await self._sync_roles_after_match(
                        season, real_players
                    )
                    await self._post_match(
                        season, fingerprint, data, changes, role_sync_result
                    )
            self._tracked.pop(room_id, None)
            self._room_rosters.pop(room_id, None)
        finally:
            self._finalizing.discard(room_id)

    async def _sync_roles_after_match(self, season, players):
        """Immediately reconcile Discord roles after one archived match."""
        if self.bot is None:
            return None

        guild = self.bot.get_guild(int(season["guild_id"]))
        if guild is None:
            return None

        role_cog = self.bot.get_cog("DiscordRoleCommands")
        if role_cog is None or not hasattr(role_cog, "sync_guild"):
            print(
                f"Could not immediately sync roles for guild {guild.id}: "
                "DiscordRoleCommands cog is unavailable."
            )
            return None

        # Match participants are the only profiles whose game totals/WR just
        # changed, so evict them before the role sync requests fresh profiles.
        for username in players:
            if isinstance(username, str):
                self.bot.profile_cache.remove(username)

        try:
            result = await role_cog.sync_guild(guild)
            print(
                f"Immediate role sync after match for guild {guild.id}: "
                f"+{result.added} / -{result.removed}"
            )
            if result.errors:
                print(
                    f"Immediate role sync warnings for guild {guild.id}: "
                    + " | ".join(result.errors)
                )
            return result
        except Exception as error:
            # Never let a Discord-role error prevent match archival/history.
            print(
                f"Immediate role sync failed for guild {guild.id}: "
                f"{type(error).__name__}: {error}"
            )
            return None

    async def _player_display_map(self, season) -> dict[str, str]:
        """Map linked ProAvalon usernames to Discord mentions for this season."""
        try:
            rows = await get_active_season_linked_users(
                int(season["guild_id"]),
                int(season["season_number"]),
            )
        except Exception:
            return {}

        return {
            str(row["proavalon_username"]).casefold():
                f"<@{int(row['discord_user_id'])}>"
            for row in rows
            if row["discord_user_id"] is not None
        }

    async def _post_match(self, season, fingerprint, data, changes, role_sync_result=None):
        if self.bot is None:
            return
        configs = await get_enabled_match_configs()
        config = next((c for c in configs if str(c["guild_id"]) == str(season["guild_id"])), None)
        if config is None:
            return
        channel = self.bot.get_channel(int(config["channel_id"]))
        if channel is None:
            try: channel = await self.bot.fetch_channel(int(config["channel_id"]))
            except discord.DiscordException: return
        winner = str(data.get("winner") or "Unknown")
        embed = discord.Embed(title=f"Community Season {season['season_number']} — Match Result", description=f"**{winner} Victory**", color=discord.Color.blurple())
        missions = data.get("missionHistory") or []
        if missions:
            icons = ["✅" if str(x).casefold()=="succeeded" else "❌" if str(x).casefold()=="failed" else "➖" for x in missions]
            embed.add_field(name="Missions", value=" ".join(icons), inline=False)
        player_update_lines: list[str] = []
        display_map = await self._player_display_map(season)
        rank_order = {
            "unranked": 0,
            "iron": 1,
            "bronze": 2,
            "silver": 3,
            "gold": 4,
            "platinum": 5,
            "diamond": 6,
            "champion": 7,
        }

        role_order = {
            # Resistance: 2 VTs, lovers, Percival, Merlin.
            "resistance": 0,
            "tristan": 1,
            "isolde": 2,
            "percival": 3,
            "merlin": 4,
            # Standard four spies.
            "assassin": 10,
            "morgana": 11,
            "mordred": 12,
            "oberon": 13,
        }
        resistance_roles = {
            "resistance", "tristan", "isolde", "percival", "merlin",
            "melron", "moregano",
        }
        spy_roles = {
            "spy", "assassin", "morgana", "mordred", "oberon",
            "hitberon", "mordredassassin",
        }

        resistance_players = []
        spy_players = []
        other_players = []

        for original_index, item in enumerate(changes):
            role = item.get("role") or "Unknown"
            role_key = str(role).casefold()
            b = item.get("before")
            a = item.get("after")
            d = item.get("change")
            rating_text = (
                "N/A"
                if b is None or a is None
                else f"{b:,} → {a:,} ({d:+d})"
            )
            line = (
                f"`{item['username']}` — **{role}** — {rating_text}"
            )
            sort_key = (role_order.get(role_key, 99), original_index)

            if role_key in resistance_roles:
                resistance_players.append((sort_key, line))
            elif role_key in spy_roles:
                spy_players.append((sort_key, line))
            else:
                other_players.append((sort_key, line))

            old = item.get("old_bracket")
            new = item.get("new_bracket")
            if old and new and str(old).casefold() != str(new).casefold():
                old_key = str(old).casefold()
                new_key = str(new).casefold()
                display = display_map.get(
                    str(item["username"]).casefold(),
                    f"`{item['username']}`",
                )
                old_value = rank_order.get(old_key)
                new_value = rank_order.get(new_key)
                if old_value is not None and new_value is not None:
                    direction = (
                        "promoted" if new_value > old_value else "demoted"
                    )
                    player_update_lines.append(
                        f"{display} has been **{direction}** to "
                        f"**{str(new).title()}**!"
                    )
                else:
                    player_update_lines.append(
                        f"{display} is now **{str(new).title()}**!"
                    )

        resistance_players.sort(key=lambda entry: entry[0])
        spy_players.sort(key=lambda entry: entry[0])
        other_players.sort(key=lambda entry: entry[0])

        if resistance_players:
            embed.add_field(
                name="Resistance",
                value="\n".join(line for _, line in resistance_players)[:1024],
                inline=False,
            )
        if spy_players:
            embed.add_field(
                name="Spies",
                value="\n".join(line for _, line in spy_players)[:1024],
                inline=False,
            )
        if other_players:
            embed.add_field(
                name="Other Players",
                value="\n".join(line for _, line in other_players)[:1024],
                inline=False,
            )

        # The Assassin public-data payload contains the assassination target.
        # It is available to spectators in the final cumulative game-data.
        public_roles = ((data.get("publicData") or {}).get("roles") or {})
        shot_names = [
            public_roles.get("assassinShotUsername"),
            public_roles.get("assassinShotUsername2"),
        ]
        shot_names = [
            name for name in shot_names
            if isinstance(name, str) and name.strip()
        ]
        if shot_names:
            roles_by_name = {
                str(item.get("username", "")).casefold():
                    (item.get("role") or "Unknown")
                for item in changes
            }
            shot_lines = []
            for name in shot_names:
                shot_role = roles_by_name.get(name.casefold(), "Unknown")
                shot_lines.append(f"`{name}` — **{shot_role}**")
            embed.add_field(
                name="Shot",
                value="\n".join(shot_lines),
                inline=False,
            )

        # Special award role ownership changes are produced by the same role
        # reconciliation that just updated Discord, so the history and actual
        # server roles stay in lock-step.
        award_update_lines: list[str] = []
        if role_sync_result is not None:
            for transition in role_sync_result.award_transitions:
                role_id = int(transition["role_id"])
                previous_ids = {int(x) for x in transition["previous_user_ids"]}
                new_ids = {int(x) for x in transition["new_user_ids"]}
                if previous_ids == new_ids:
                    continue

                role_mention = f"<@&{role_id}>"
                gained = sorted(new_ids - previous_ids)
                lost = sorted(previous_ids - new_ids)
                retained = sorted(new_ids & previous_ids)

                def mentions(ids: list[int]) -> str:
                    return ", ".join(f"<@{user_id}>" for user_id in ids)

                if new_ids and not previous_ids:
                    holders = mentions(sorted(new_ids))
                    verb = "is" if len(new_ids) == 1 else "are"
                    award_update_lines.append(
                        f"{holders} {verb} now {role_mention}!"
                    )
                elif gained and lost:
                    holders = mentions(sorted(new_ids))
                    verb = "is" if len(new_ids) == 1 else "are"
                    previous = mentions(lost)
                    owner_label = (
                        "Previous owner" if len(lost) == 1
                        else "Previous owners"
                    )
                    award_update_lines.append(
                        f"{holders} {verb} now {role_mention}! "
                        f"**{owner_label}:** {previous}"
                    )
                elif gained:
                    newcomers = mentions(gained)
                    if retained:
                        award_update_lines.append(
                            f"{newcomers} has joined {mentions(retained)} "
                            f"as {role_mention}!"
                        )
                    else:
                        award_update_lines.append(
                            f"{newcomers} is now {role_mention}!"
                        )
                elif lost and new_ids:
                    holders = mentions(sorted(new_ids))
                    award_update_lines.append(
                        f"{holders} now holds {role_mention}. "
                        f"**Previous co-holder(s):** {mentions(lost)}"
                    )
                elif lost and not new_ids:
                    award_update_lines.append(
                        f"{role_mention} is now vacant. "
                        f"**Previous owner(s):** {mentions(lost)}"
                    )

        if player_update_lines or award_update_lines:
            combined_updates = list(player_update_lines)
            if player_update_lines and award_update_lines:
                combined_updates.append("──────────")
            combined_updates.extend(award_update_lines)
            embed.add_field(
                name="Player Updates",
                value="\n".join(combined_updates)[:1024],
                inline=False,
            )
        if data.get("voteHistory") is None:
            embed.add_field(name="Voting History", value="Vote history was disabled for this match.", inline=False)
        embed.set_footer(text=f"Room {data.get('roomId')} • Automatically archived")
        chart = build_vote_history_png(data)
        try:
            if chart:
                file=discord.File(BytesIO(chart), filename="voting-history.png")
                embed.set_image(url="attachment://voting-history.png")
                await channel.send(embed=embed,file=file)
            else:
                await channel.send(embed=embed)
            await mark_match_posted(int(season["id"]),fingerprint)
        except discord.DiscordException as error:
            print(f"Could not post season match: {error}")
