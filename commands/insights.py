from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import re
import discord
from discord import app_commands
from discord.ext import commands
from database.insights import active_or_latest,season_rows,recent_rows,find_match,registered_usernames,all_links_for_discord,bot_status_data,unpack,player_summary,streak_summary,role_summary,alignment_summary,role_board,activity_summary,season_records,season_overview_summary,ratings_for_usernames,relationships,season_match_player_names,season_player_discord_links,search_matches_by_players,guild_match_player_names,search_guild_matches_by_players,winner_norm
from utils.permissions import bot_admin_only
from database.account_links import get_preferred_link_for_discord
from database.social_stats import build_player_social_stats, get_season_matches
from database.assassination_stats import (
    ROLE_BUCKETS,
    get_player_merlin_shot_stats,
)
from commands.stats import (
    HISTORY_ROLE_CHOICES,
    SOCIAL_LIST_CHOICES,
    SocialListView,
    build_social_embed,
)
from utils.discord_users import parse_discord_user_id
from database.seasons import (
    get_season_by_prefix,
    season_display_name,
)

def fmt_date(v):
    if not v: return 'N/A'
    try: return datetime.fromisoformat(str(v).replace('Z','+00:00')).strftime('%b %d, %Y')
    except Exception: return str(v)[:10]


def fmt_duration(seconds):
    if seconds is None:
        return "N/A"
    total = max(0, int(round(float(seconds))))
    hours, remainder = divmod(total, 3600)
    minutes, remaining_seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {remaining_seconds}s"
    return f"{minutes}m {remaining_seconds}s"


def format_outcome_split(values) -> str:
    games, resistance_wins, spy_wins = (int(value) for value in values)
    known = resistance_wins + spy_wins
    resistance_rate = resistance_wins / known * 100 if known else 0.0
    spy_rate = spy_wins / known * 100 if known else 0.0
    return (
        f"{games:,} games • Res WR {resistance_rate:.1f}% • "
        f"Spy WR {spy_rate:.1f}%"
    )

async def player_auto(interaction,current):
    if interaction.guild_id is None: return []
    s=await active_or_latest(interaction.guild_id)
    if not s: return []
    vals=await registered_usernames(s['id']); q=current.casefold()
    return [app_commands.Choice(name=v,value=v) for v in vals if q in v.casefold()][:25]

MATCH_SEARCH_PAGE_SIZE = 5
DISCORD_MENTION_RE = re.compile(r"^<@!?(\d+)>$")


def _compact_match_value(row) -> str:
    participants = unpack(row)
    resistance = [p["username"] for p in participants if p["alignment"] == "Resistance"]
    spies = [p["username"] for p in participants if p["alignment"] == "Spy"]
    unknown = [p["username"] for p in participants if p["alignment"] is None]

    lines = [
        f"**Res:** {', '.join(f'`{name}`' for name in resistance) or 'N/A'}",
        f"**Spy:** {', '.join(f'`{name}`' for name in spies) or 'N/A'}",
    ]
    if unknown:
        lines.append(f"**Other:** {', '.join(f'`{name}`' for name in unknown)}")
    if row["youtube_url"]:
        lines.append(f"[YouTube]({row['youtube_url']})")
    return "\n".join(lines)


class MatchSearchView(discord.ui.View):
    def __init__(self, *, rows, searched_names, requesting_user_id: int):
        super().__init__(timeout=180)
        self.rows = list(rows)
        self.searched_names = searched_names
        self.requesting_user_id = requesting_user_id
        self.page = 0
        self.total_pages = max(1, math.ceil(len(self.rows) / MATCH_SEARCH_PAGE_SIZE))
        self._update_buttons()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requesting_user_id:
            return True
        await interaction.response.send_message(
            "Only the person who opened this search can change pages.",
            ephemeral=True,
        )
        return False

    def _update_buttons(self) -> None:
        self.previous_button.disabled = self.page == 0
        self.next_button.disabled = self.page >= self.total_pages - 1

    def build_embed(self) -> discord.Embed:
        start = self.page * MATCH_SEARCH_PAGE_SIZE
        page_rows = self.rows[start:start + MATCH_SEARCH_PAGE_SIZE]
        embed = discord.Embed(
            title="All Seasons — Match Search",
            description=(
                f"**Players:** {', '.join(f'`{name}`' for name in self.searched_names)}\n"
                f"**{len(self.rows)} matching match{'es' if len(self.rows) != 1 else ''}**"
            ),
            color=discord.Color.blurple(),
        )
        for row in page_rows:
            winner = str(row["winner"] or "Unknown")
            embed.add_field(
                name=(
                    f"{row['season_display_name']} • #{row['season_match_id']} • "
                    f"{winner} win • "
                    f"{fmt_date(row['finished_at'])}"
                ),
                value=_compact_match_value(row),
                inline=False,
            )
        embed.set_footer(text=f"Page {self.page + 1}/{self.total_pages}")
        return embed

    @discord.ui.button(label="Previous", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page > 0:
            self.page -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Next", emoji="▶️", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page < self.total_pages - 1:
            self.page += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True


async def match_search_autocomplete(interaction: discord.Interaction, current: str):
    if interaction.guild_id is None:
        return []
    names = await guild_match_player_names(interaction.guild_id)
    selected = {
        str(getattr(interaction.namespace, f"player_{index}", "") or "")
        .strip()
        .casefold()
        for index in range(1, 11)
    }
    query = current.strip().lstrip("@").casefold()
    choices = []
    for name in names:
        key = name.casefold()
        if key in selected and key != current.strip().casefold():
            continue
        choice_name = name
        if query and query not in key:
            continue
        choices.append(app_commands.Choice(name=choice_name[:100], value=name))
        if len(choices) == 25:
            break
    return choices

async def resolve_name(
    interaction,
    username: str | None = None,
    discord_user: discord.Member | None = None,
):
    if interaction.guild_id is None:
        return None, None, "This command can only be used in a server."

    season = await active_or_latest(interaction.guild_id)
    if not season:
        return None, None, "No season history exists."

    if username is not None and username.strip() and discord_user is not None:
        return None, None, "Choose either `username` or `discord_user`, not both."

    if username is not None and username.strip():
        requested = username.strip()
    else:
        target = discord_user or interaction.user
        link = await get_preferred_link_for_discord(
            interaction.guild_id,
            target.id,
            int(season["season_number"]),
        )
        if link is None:
            return (
                None,
                None,
                f"{target.mention} is not linked to an account for "
                f"{season_display_name(season)}.",
            )
        requested = str(link["proavalon_username"])

    vals = await registered_usernames(season["id"])
    hit = next(
        (v for v in vals if v.casefold() == requested.casefold()),
        requested,
    )
    return season, hit, None

class Insights(commands.Cog):
    def __init__(self,bot): self.bot=bot

    @app_commands.command(name='recent',description='Shows the most recent archived season matches.')
    async def recent(self,interaction:discord.Interaction,count:app_commands.Range[int,1,15]=5):
        s=await active_or_latest(interaction.guild_id)
        if not s: return await interaction.response.send_message('No season history exists.',ephemeral=True)
        rows=await recent_rows(s['id'],count)
        if not rows: return await interaction.response.send_message('No archived matches yet.',ephemeral=True)
        e=discord.Embed(title=f"{season_display_name(s)} — Recent Matches",color=discord.Color.blurple())
        for r in rows:
            players=json.loads(r['players_json'])
            recording=(f"\n[Watch recording]({r['youtube_url']})" if r['youtube_url'] else "")
            e.add_field(name=f"Match #{r['season_match_id']} • {fmt_date(r['finished_at'])}",value=f"**{r['winner'] or 'Unknown'} Victory**\n"+', '.join(players)+recording,inline=False)
        await interaction.response.send_message(embed=e)

    @app_commands.command(
        name="match-search",
        description="Finds archived matches containing all supplied players.",
    )
    @app_commands.describe(
        player_1="First player (required).",
        player_2="Second player.",
        player_3="Third player.",
        player_4="Fourth player.",
        player_5="Fifth player.",
        player_6="Sixth player.",
        player_7="Seventh player.",
        player_8="Eighth player.",
        player_9="Ninth player.",
        player_10="Tenth player.",
    )
    @app_commands.autocomplete(
        player_1=match_search_autocomplete,
        player_2=match_search_autocomplete,
        player_3=match_search_autocomplete,
        player_4=match_search_autocomplete,
        player_5=match_search_autocomplete,
        player_6=match_search_autocomplete,
        player_7=match_search_autocomplete,
        player_8=match_search_autocomplete,
        player_9=match_search_autocomplete,
        player_10=match_search_autocomplete,
    )
    async def match_search(
        self,
        interaction: discord.Interaction,
        player_1: app_commands.Range[str, 1, 100],
        player_2: app_commands.Range[str, 1, 100] | None = None,
        player_3: app_commands.Range[str, 1, 100] | None = None,
        player_4: app_commands.Range[str, 1, 100] | None = None,
        player_5: app_commands.Range[str, 1, 100] | None = None,
        player_6: app_commands.Range[str, 1, 100] | None = None,
        player_7: app_commands.Range[str, 1, 100] | None = None,
        player_8: app_commands.Range[str, 1, 100] | None = None,
        player_9: app_commands.Range[str, 1, 100] | None = None,
        player_10: app_commands.Range[str, 1, 100] | None = None,
    ):
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )

        raw_entries = [
            entry.strip()
            for entry in (
                player_1, player_2, player_3, player_4, player_5,
                player_6, player_7, player_8, player_9, player_10,
            )
            if entry and entry.strip()
        ]

        season = await active_or_latest(interaction.guild_id)
        if not season:
            return await interaction.response.send_message(
                "No season history exists.", ephemeral=True
            )

        await interaction.response.defer(thinking=True)
        archived_names = await guild_match_player_names(interaction.guild_id)
        canonical = {name.casefold(): name for name in archived_names}
        resolved = []
        unresolved = []

        for entry in raw_entries:
            mention = DISCORD_MENTION_RE.fullmatch(entry)
            if mention:
                link = await get_preferred_link_for_discord(
                    interaction.guild_id,
                    int(mention.group(1)),
                    int(season["season_number"]),
                )
                if link is None:
                    unresolved.append(entry)
                    continue
                requested = str(link["proavalon_username"]).strip()
            else:
                requested = entry

            name = canonical.get(requested.casefold())
            if name is None:
                unresolved.append(entry)
            elif name.casefold() not in {item.casefold() for item in resolved}:
                resolved.append(name)

        if unresolved:
            return await interaction.followup.send(
                "I couldn't find an archived player for: "
                + ", ".join(f"`{item}`" for item in unresolved)
                + ". Mentions must be linked to a ProAvalon account.",
                ephemeral=True,
            )
        if not resolved:
            return await interaction.followup.send(
                "No valid players were supplied.", ephemeral=True
            )

        rows = await search_guild_matches_by_players(interaction.guild_id, resolved)
        if not rows:
            return await interaction.followup.send(
                "No archived matches contain every supplied player.", ephemeral=True
            )

        view = MatchSearchView(
            rows=rows,
            searched_names=resolved,
            requesting_user_id=interaction.user.id,
        )
        await interaction.followup.send(embed=view.build_embed(), view=view)

    @app_commands.command(name='match',description='Shows one archived match by its season match ID.')
    async def match(self,interaction:discord.Interaction,match_id:int):
        s=await active_or_latest(interaction.guild_id)
        if not s: return await interaction.response.send_message('No season history exists.',ephemeral=True)
        r=await find_match(s['id'],match_id)
        if not r: return await interaction.response.send_message('That match was not found in this season.',ephemeral=True)
        parts=unpack(r); res=[p for p in parts if p['alignment']=='Resistance']; spies=[p for p in parts if p['alignment']=='Spy']
        e=discord.Embed(title=f"Match #{r['season_match_id']} — {r['winner'] or 'Unknown'} Victory",description=f"{fmt_date(r['finished_at'])}",color=discord.Color.blurple())
        e.add_field(name='Resistance',value='\n'.join(f"`{p['username']}` — {p['role'] or 'Unknown'}" for p in res) or 'N/A',inline=False)
        e.add_field(name='Spies',value='\n'.join(f"`{p['username']}` — {p['role'] or 'Unknown'}" for p in spies) or 'N/A',inline=False)
        missions=json.loads(r['mission_history_json']); e.add_field(name='Missions',value=' '.join('✅' if str(x).casefold()=='succeeded' else '❌' for x in missions) or 'N/A',inline=False)
        if r['youtube_url']:
            e.add_field(name='Recording',value=f"[Watch the full match on YouTube]({r['youtube_url']})",inline=False)
        await interaction.response.send_message(embed=e)

    @app_commands.command(
        name="current-match",
        description="Shows qualifying community-season matches currently in progress.",
    )
    async def current_match(self, interaction: discord.Interaction):
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )

        leaderboard_cog = self.bot.get_cog("LeaderboardCommands")
        tracker = getattr(leaderboard_cog, "rating_tracker", None)
        if tracker is None:
            return await interaction.response.send_message(
                "The live match tracker is not available right now.",
                ephemeral=True,
            )

        await interaction.response.defer(thinking=True)
        snapshots = await tracker.get_current_match_snapshots(
            interaction.guild_id
        )
        if not snapshots:
            return await interaction.followup.send(
                "No qualifying community-season match is currently in progress."
            )

        embeds = []
        now = datetime.now(timezone.utc)
        for index, snapshot in enumerate(snapshots, 1):
            room_id = snapshot["room_id"]
            game = snapshot.get("game") or {}
            data = snapshot.get("data") or {}
            players = snapshot.get("players") or []
            missions = data.get("missionHistory") or game.get("missionHistory") or []
            fails = data.get("numFailsHistory") or game.get("numFailsHistory") or []

            succeeded = sum(
                str(result).casefold() == "succeeded" for result in missions
            )
            failed = sum(
                str(result).casefold() == "failed" for result in missions
            )
            title = "Current Match"
            if len(snapshots) > 1:
                title += f" {index}"
            embed = discord.Embed(
                title=title,
                description=f"**In Progress** • Room `{room_id}`",
                color=discord.Color.blurple(),
            )

            started_at = snapshot.get("started_at")
            if started_at is not None:
                elapsed = max(0, (now - started_at).total_seconds())
                embed.add_field(
                    name="Game Time",
                    value=f"**{tracker._format_duration(elapsed)}**",
                    inline=True,
                )
            if game.get("gameMode"):
                embed.add_field(
                    name="Mode", value=str(game["gameMode"]), inline=True
                )
            embed.add_field(
                name="Score",
                value=f"Resistance **{succeeded}** — **{failed}** Spies",
                inline=True,
            )

            mission_parts = []
            for mission_index, result in enumerate(missions):
                normalized = str(result).casefold()
                icon = "✅" if normalized == "succeeded" else "❌" if normalized == "failed" else "➖"
                try:
                    fail_count = int(fails[mission_index])
                except (IndexError, TypeError, ValueError):
                    fail_count = 0
                fail_text = f" ({fail_count}F)" if fail_count > 0 else ""
                mission_parts.append(
                    f"M{mission_index + 1} {icon}{fail_text}"
                )
            if succeeded < 3 and failed < 3:
                mission_parts.append(f"**Current: M{min(5, len(missions) + 1)}**")
            embed.add_field(
                name="Missions",
                value=" • ".join(mission_parts) if mission_parts else "**Current: M1**",
                inline=False,
            )
            if players:
                embed.add_field(
                    name=f"Players ({len(players)})",
                    value="\n".join(f"`{name}`" for name in players)[:1024],
                    inline=False,
                )

            public_cards = ((data.get("publicData") or {}).get("cards") or {})
            if isinstance(public_cards, dict):
                for card_key, card_data in public_cards.items():
                    if not isinstance(card_data, dict):
                        continue
                    card_name = str(card_data.get("name") or card_key or "")
                    normalized = card_name.replace("_", " ").replace("-", " ").casefold()
                    if "ref" not in normalized or "rain" not in normalized:
                        continue
                    history = [
                        str(name) for name in (card_data.get("history") or [])
                        if isinstance(name, str) and name.strip()
                    ]
                    if history:
                        embed.add_field(
                            name="Ref of the Rain",
                            value=(
                                f"**Current:** `{history[-1]}`\n"
                                + " → ".join(f"`{name}`" for name in history)
                            )[:1024],
                            inline=False,
                        )
                    break

            embed.set_footer(text="Live public snapshot • no hidden roles are shown")
            embeds.append(embed)

        await interaction.followup.send(embeds=embeds[:10])

    @app_commands.command(name='streaks',description='Shows current and longest win/loss streaks.')
    @app_commands.describe(username='ProAvalon username.', discord_user='Linked Discord @ to look up.')
    @app_commands.autocomplete(username=player_auto)
    async def streaks(self,interaction:discord.Interaction,username:str|None=None,discord_user:discord.Member|None=None):
        s,u,error=await resolve_name(interaction,username,discord_user)
        if error: return await interaction.response.send_message(error,ephemeral=True)
        rows=await season_rows(s['id']); x=streak_summary(rows,u)
        if not x['games']: return await interaction.response.send_message('No archived matches found for that player.',ephemeral=True)
        await interaction.response.send_message(embed=discord.Embed(title=f'{u} — Streaks',description=f"Current: **{x['current']} {x['current_kind']}**\nLongest win streak: **{x['longest_win']}**\nLongest loss streak: **{x['longest_loss']}**",color=discord.Color.blurple()))

    @app_commands.command(name='records',description='Shows season-wide statistical records.')
    @app_commands.describe(minimum_games='Minimum games required for rate-based records.')
    async def records(self,interaction:discord.Interaction,minimum_games:app_commands.Range[int,1,999]=5):
        s=await active_or_latest(interaction.guild_id)
        if not s:
            return await interaction.response.send_message('No season history exists.',ephemeral=True)

        rows=await season_rows(s['id'])
        x=season_records(rows,minimum_games)
        ratings=await ratings_for_usernames(x['usernames'])

        rated=[]
        for username in x['usernames']:
            rating_row=ratings.get(username.casefold())
            if rating_row is not None and rating_row['rating'] is not None:
                rated.append((username,int(rating_row['rating']),rating_row['rating_bracket']))

        top_rated=max(rated,key=lambda item:item[1],default=None)
        bottom_rated=min(rated,key=lambda item:item[1],default=None)

        e=discord.Embed(
            title=f"{season_display_name(s)} — Records",
            description=(
                f"**{x['matches']} matches** • **{x['unique_players']} players** • "
                f"Rate records require **{minimum_games} games**"
            ),
            color=discord.Color.blurple(),
        )

        def overall_line(v):
            if not v: return 'N/A'
            return f"`{v['username']}` — {v['games']} GP • {v['wr']:.1f}% WR"

        def alignment_line(v,key):
            if not v: return 'N/A'
            a=v[key]
            return f"`{v['username']}` — {a['wins']}W-{a['losses']}L • {a['wr']:.1f}% WR • {a['games']} GP"

        def streak_line(v):
            if not v: return 'N/A'
            return f"`{v['username']}` — {v['longest_win_streak']} games"

        def rating_line(v):
            if not v: return 'No known ratings yet.'
            username,rating,bracket=v
            bracket_text=f" • {str(bracket).title()}" if bracket else ''
            return f"`{username}` — **{rating:,}**{bracket_text}"

        e.add_field(name='Top Rated Player',value=rating_line(top_rated),inline=False)
        e.add_field(name='Bottom Rated Player',value=rating_line(bottom_rated),inline=False)
        e.add_field(name='Most Games Played',value=overall_line(x['most_games']),inline=False)
        e.add_field(name='Best Overall WR',value=overall_line(x['best_wr']),inline=False)
        e.add_field(name='Worst Overall WR',value=overall_line(x['worst_wr']),inline=False)
        e.add_field(name='Highest Win Streak',value=streak_line(x['highest_win_streak']),inline=False)
        e.add_field(name='Lowest Win Streak',value=streak_line(x['lowest_win_streak']),inline=False)
        e.add_field(name='Best Resistance',value=alignment_line(x['best_res'],'res'),inline=False)
        e.add_field(name='Worst Resistance',value=alignment_line(x['worst_res'],'res'),inline=False)
        e.add_field(name='Best Spy',value=alignment_line(x['best_spy'],'spy'),inline=False)
        e.add_field(name='Worst Spy',value=alignment_line(x['worst_spy'],'spy'),inline=False)

        await interaction.response.send_message(embed=e)

    @app_commands.command(name='role-stats',description='Shows a player’s archived performance by role.')
    @app_commands.describe(username='ProAvalon username.', discord_user='Linked Discord @ to look up.')
    @app_commands.autocomplete(username=player_auto)
    async def role_stats(self,interaction:discord.Interaction,username:str|None=None,discord_user:discord.Member|None=None):
        s,u,error=await resolve_name(interaction,username,discord_user)
        if error: return await interaction.response.send_message(error,ephemeral=True)
        data=role_summary(await season_rows(s['id']),u)
        if not data: return await interaction.response.send_message('No archived role data found.',ephemeral=True)
        await interaction.response.send_message(embed=discord.Embed(title=f'{u} — Role Stats',color=discord.Color.blurple(),description='\n'.join(f"**{x['role']}** — {x['wins']}W-{x['losses']}L • {x['wr']:.1f}% • {x['games']} GP" for x in data)))

    @app_commands.command(name='role-leaderboard',description='Ranks season players for one role.')
    async def role_leaderboard(self,interaction:discord.Interaction,role:str,minimum_games:app_commands.Range[int,1,999]=3):
        s=await active_or_latest(interaction.guild_id); data=role_board(await season_rows(s['id']),role) if s else []; data=[x for x in data if x['games']>=minimum_games]; data.sort(key=lambda x:(-x['wr'],-x['games'],x['username'].casefold()))
        if not data: return await interaction.response.send_message('No qualifying role data found.',ephemeral=True)
        lines=[f"{i}. `{x['username']}` — {x['games']} GP • {x['wr']:.1f}% WR" for i,x in enumerate(data[:25],1)]
        await interaction.response.send_message(embed=discord.Embed(title=f"{season_display_name(s)} — {role.title()} Leaderboard",description='\n'.join(lines),color=discord.Color.blurple()))

    @app_commands.command(name='alignment-stats',description='Shows a player’s Resistance vs Spy performance.')
    @app_commands.describe(username='ProAvalon username.', discord_user='Linked Discord @ to look up.')
    @app_commands.autocomplete(username=player_auto)
    async def alignment_stats(self,interaction:discord.Interaction,username:str|None=None,discord_user:discord.Member|None=None):
        s,u,error=await resolve_name(interaction,username,discord_user)
        if error: return await interaction.response.send_message(error,ephemeral=True)
        x=alignment_summary(await season_rows(s['id']),u)
        e=discord.Embed(title=f'{u} — Alignment Stats',color=discord.Color.blurple())
        for k in ('Resistance','Spy'):
            v=x.get(k,{'games':0,'wins':0,'losses':0,'wr':0}); e.add_field(name=k,value=f"{v['wins']}W-{v['losses']}L • {v['wr']:.1f}% WR • {v['games']} GP",inline=False)
        await interaction.response.send_message(embed=e)

    @app_commands.command(name='activity',description='Shows recent season activity for a player.')
    @app_commands.describe(username='ProAvalon username.', discord_user='Linked Discord @ to look up.', days='Number of recent days to analyze. Defaults to 7.')
    @app_commands.autocomplete(username=player_auto)
    async def activity(self,interaction:discord.Interaction,username:str|None=None,discord_user:discord.Member|None=None,days:app_commands.Range[int,1,365]=7):
        s,u,error=await resolve_name(interaction,username,discord_user)
        if error: return await interaction.response.send_message(error,ephemeral=True)
        x=activity_summary(await season_rows(s['id']),u,days)
        if not x: return await interaction.response.send_message('No data found.',ephemeral=True)
        wr=x['wins']/x['games']*100 if x['games'] else 0
        await interaction.response.send_message(embed=discord.Embed(title=f'{u} — Activity',description=f"Last {days} days: **{x['games']} games** • **{wr:.1f}% WR**\nBusiest day: **{x['busiest_day'] or 'N/A'}** ({x['busiest_count']} games)\nLast tracked game: **{fmt_date(x['last'])}**",color=discord.Color.blurple()))

    @app_commands.command(name='player-history',description='Shows remembered accounts and archived results across seasons.')
    async def player_history(self,interaction:discord.Interaction,discord_user:discord.Member|None=None,discord_user_id:str|None=None):
        if discord_user is None and not discord_user_id: return await interaction.response.send_message('Provide `discord_user` or `discord_user_id`.',ephemeral=True)
        if discord_user is not None and discord_user_id: return await interaction.response.send_message('Choose only one Discord lookup method.',ephemeral=True)
        uid=discord_user.id if discord_user else parse_discord_user_id(discord_user_id); links=await all_links_for_discord(interaction.guild_id,uid)
        if not links: return await interaction.response.send_message('No remembered account links found.',ephemeral=True)
        lines=[]
        for l in links:
            rows=await season_rows(l['community_season_id']) if l['community_season_id'] else []; x=player_summary(rows,l['proavalon_username']); lines.append(f"**{l['display_name'] or l['required_prefix'] or 'General'}** — `{l['proavalon_username']}` • {x['games']} GP • {x['wr']:.1f}% WR")
        embed=discord.Embed(
            title='Player History',
            description=f'<@{uid}>\n\n' + '\n'.join(lines),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )


    @app_commands.command(name='bot-status',description='Shows this server’s bot configuration and tracker status.')
    @bot_admin_only()
    async def show_bot_status(self,interaction:discord.Interaction):
        x=await bot_status_data(interaction.guild_id); s=x['active']; e=discord.Embed(title='ProAvalon Stats — Bot Status',color=discord.Color.blurple())
        e.add_field(name='Active Season',value='None' if not s else f"**{season_display_name(s)}** • Prefix `{s['required_prefix']}`",inline=False)
        e.add_field(name='Tracked Data',value=f"{x['players']} players • {x['matches']} archived matches",inline=False)
        e.add_field(name='Commands Channel',value='Not set' if not x['commands'] else f"<#{x['commands']['channel_id']}>",inline=True)
        e.add_field(name='Feedback Channel',value='Not set' if not x['feedback'] else f"<#{x['feedback']['channel_id']}>",inline=True)
        e.add_field(name='Daily Leaderboard',value='Disabled/not set' if not x['daily'] or not x['daily']['enabled'] else f"Enabled • <#{x['daily']['channel_id']}>",inline=False)
        e.add_field(name='Match Posting',value='Disabled/not set' if not x['matchcfg'] or not x['matchcfg']['enabled'] else f"Enabled • <#{x['matchcfg']['channel_id']}>",inline=False)
        await interaction.response.send_message(embed=e)


class PastSeason(commands.GroupCog, group_name="past-season", group_description="Browse archived community seasons."):
    def __init__(self, bot):
        self.bot = bot

    async def _season(self, interaction: discord.Interaction, prefix: str):
        if interaction.guild_id is None:
            return None
        return await get_season_by_prefix(interaction.guild_id, prefix)

    async def _player(
        self,
        interaction: discord.Interaction,
        season,
        username: str | None,
        discord_user: discord.Member | None,
    ):
        if username and username.strip() and discord_user is not None:
            return None, "Choose either `username` or `discord_user`, not both."
        if username and username.strip():
            requested = username.strip()
        else:
            target = discord_user or interaction.user
            link = await get_preferred_link_for_discord(
                interaction.guild_id,
                target.id,
                int(season["season_number"]),
            )
            if link is None:
                return None, (
                    f"{target.mention} is not linked to an account for "
                    f"{season_display_name(season)}."
                )
            requested = str(link["proavalon_username"])

        names = await season_match_player_names(int(season["id"]))
        resolved = next(
            (name for name in names if name.casefold() == requested.casefold()),
            None,
        )
        if resolved is None:
            return None, (
                f"`{requested}` has no preserved matches in "
                f"{season_display_name(season)}."
            )
        return resolved, None

    @app_commands.command(name="player", description="Shows a player's archived statistics from a previous season.")
    @app_commands.describe(
        season_prefix="Season prefix, for example s4.",
        username="Player name. Leave blank to use your linked account.",
        discord_user="Linked Discord user to inspect.",
    )
    async def player(
        self,
        interaction: discord.Interaction,
        season_prefix: str,
        username: str | None = None,
        discord_user: discord.Member | None = None,
    ):
        season = await self._season(interaction, season_prefix)
        if not season:
            return await interaction.response.send_message("That season prefix was not found.", ephemeral=True)
        resolved, error = await self._player(interaction, season, username, discord_user)
        if error:
            return await interaction.response.send_message(error, ephemeral=True)

        rows = await season_rows(int(season["id"]))
        overall = player_summary(rows, resolved)
        alignments = alignment_summary(rows, resolved)
        streaks = streak_summary(rows, resolved)
        roles = role_summary(rows, resolved)
        embed = discord.Embed(
            title=f"{resolved} — {season_display_name(season)}",
            description=(
                f"**Overall:** {overall['wins']}W–{overall['losses']}L "
                f"• {overall['wr']:.1f}% WR • {overall['games']} games\n"
                f"**Current streak:** {streaks['current']} {streaks['current_kind']}\n"
                f"**Longest win streak:** {streaks['longest_win']}\n"
                f"**Longest loss streak:** {streaks['longest_loss']}"
            ),
            color=discord.Color.blurple(),
        )
        for alignment_name in ("Resistance", "Spy"):
            item = alignments[alignment_name]
            embed.add_field(
                name=alignment_name,
                value=f"{item['wins']}W–{item['losses']}L • {item['wr']:.1f}% WR • {item['games']} games",
                inline=False,
            )
        role_lines = [
            f"**{item['role']}** — {item['wins']}W–{item['losses']}L • {item['wr']:.1f}% WR • {item['games']} games"
            for item in roles
        ]
        embed.add_field(name="Roles", value="\n".join(role_lines)[:1024] or "N/A", inline=False)
        embed.set_footer(text="Archived match data; live ratings and deleted voting details are excluded.")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="history", description="Shows archived wins and losses from a previous season.")
    @app_commands.describe(
        season_prefix="Season prefix, for example s4.",
        username="Player name. Leave blank to use your linked account.",
        discord_user="Linked Discord user to inspect.",
        games="Number of recent qualifying games to show.",
        role="Optionally filter by role or alignment.",
    )
    @app_commands.choices(role=HISTORY_ROLE_CHOICES)
    async def history(
        self,
        interaction: discord.Interaction,
        season_prefix: str,
        username: str | None = None,
        discord_user: discord.Member | None = None,
        games: app_commands.Range[int, 1, 250] = 10,
        role: app_commands.Choice[str] | None = None,
    ):
        season = await self._season(interaction, season_prefix)
        if not season:
            return await interaction.response.send_message("That season prefix was not found.", ephemeral=True)
        resolved, error = await self._player(interaction, season, username, discord_user)
        if error:
            return await interaction.response.send_message(error, ephemeral=True)

        rows = await season_rows(int(season["id"]))
        role_filter = role.value.casefold() if role else None
        results = []
        for row in reversed(rows):
            participant = next((p for p in unpack(row) if p["username"].casefold() == resolved.casefold()), None)
            if participant is None or participant["alignment"] is None:
                continue
            if role_filter:
                stored = str(participant["role"] or "").replace(" ", "").casefold()
                matches = (
                    participant["alignment"] == "Resistance" if role_filter == "overall-resistance"
                    else participant["alignment"] == "Spy" if role_filter == "overall-spy"
                    else stored == "resistance" if role_filter == "vt"
                    else stored in {"tristan", "isolde"} if role_filter == "lover"
                    else stored == role_filter
                )
                if not matches:
                    continue
            winner = winner_norm(row["winner"])
            if winner:
                results.append(participant["alignment"] == winner)

        selected = results[:int(games)]
        if not selected:
            return await interaction.response.send_message("No qualifying archived results were found.", ephemeral=True)
        wins = sum(selected)
        icons = ["✅" if won else "❌" for won in selected]
        lines = [" ".join(icons[i:i + 25]) for i in range(0, len(icons), 25)]
        embed = discord.Embed(
            title=f"{resolved} — {season_display_name(season)} History",
            description=(
                f"**{role.name if role else 'Overall'} • {len(selected)} most recent**\n"
                + "\n".join(lines)
                + f"\n\n**{wins}W–{len(selected) - wins}L • {wins / len(selected) * 100:.1f}% WR**"
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="Newest → oldest")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="social", description="Shows archived social statistics from a previous season.")
    @app_commands.describe(
        season_prefix="Season prefix, for example s4.",
        username="Player name. Leave blank to use your linked account.",
        discord_user="Linked Discord user to inspect.",
    )
    async def social(
        self,
        interaction: discord.Interaction,
        season_prefix: str,
        username: str | None = None,
        discord_user: discord.Member | None = None,
    ):
        season = await self._season(interaction, season_prefix)
        if not season:
            return await interaction.response.send_message("That season prefix was not found.", ephemeral=True)
        resolved, error = await self._player(interaction, season, username, discord_user)
        if error:
            return await interaction.response.send_message(error, ephemeral=True)
        matches = await get_season_matches(int(season["id"]))
        stats = build_player_social_stats(matches, resolved, minimum_shared_games=3)
        if stats is None:
            return await interaction.response.send_message("No archived social statistics were found.", ephemeral=True)
        await interaction.response.send_message(
            embed=build_social_embed(season_name=season_display_name(season), stats=stats)
        )

    @app_commands.command(name="list", description="Lists archived teammates and rivals from a previous season.")
    @app_commands.describe(
        season_prefix="Season prefix, for example s4.",
        category="Relationship list to show.",
        username="Player name. Leave blank to use your linked account.",
        discord_user="Linked Discord user to inspect.",
        minimum_games="Minimum shared games required.",
        results_per_page="Results shown on each page.",
    )
    @app_commands.choices(category=SOCIAL_LIST_CHOICES)
    async def relationship_list(
        self,
        interaction: discord.Interaction,
        season_prefix: str,
        category: app_commands.Choice[str],
        username: str | None = None,
        discord_user: discord.Member | None = None,
        minimum_games: app_commands.Range[int, 1, 999] = 1,
        results_per_page: app_commands.Range[int, 5, 25] = 10,
    ):
        season = await self._season(interaction, season_prefix)
        if not season:
            return await interaction.response.send_message("That season prefix was not found.", ephemeral=True)
        resolved, error = await self._player(interaction, season, username, discord_user)
        if error:
            return await interaction.response.send_message(error, ephemeral=True)

        matches = await get_season_matches(int(season["id"]))
        stats = build_player_social_stats(matches, resolved, minimum_shared_games=1)
        if stats is None:
            return await interaction.response.send_message("No archived social statistics were found.", ephemeral=True)
        items = list(stats["relationships"])
        minimum = int(minimum_games)
        value = category.value

        if value == "most-played-with":
            items = [x for x in items if int(x["shared_games"]) >= minimum]
            items.sort(key=lambda x: (-int(x["shared_games"]), str(x["username"]).casefold()))
            title = f"{resolved} — Most Played With"
            lines = [f"**{i}.** `{x['username']}` — **{x['shared_games']} shared** • {x['same_games']} same • {x['opposite_games']} opposite" for i, x in enumerate(items, 1)]
        elif value == "teammates":
            items = [x for x in items if int(x["same_games"]) >= minimum]
            items.sort(key=lambda x: (-int(x["same_games"]), -float(x["same_win_rate"]), str(x["username"]).casefold()))
            title = f"{resolved} — Teammates"
            lines = [f"**{i}.** `{x['username']}` — **{x['same_games']} together** • {x['same_win_rate']:.1f}% WR" for i, x in enumerate(items, 1)]
        elif value in {"best-teammates", "best-resistance-teammates", "best-spy-teammates"}:
            if value == "best-resistance-teammates":
                games_key, wins_key, losses_key, wr_key, label = "res_games", "res_wins", "res_losses", "res_win_rate", "Best Resistance Teammates"
            elif value == "best-spy-teammates":
                games_key, wins_key, losses_key, wr_key, label = "spy_games", "spy_wins", "spy_losses", "spy_win_rate", "Best Spy Teammates"
            else:
                games_key, wins_key, losses_key, wr_key, label = "same_games", "same_wins", "same_losses", "same_win_rate", "Best Teammates"
            items = [x for x in items if int(x[games_key]) >= minimum]
            items.sort(key=lambda x: (-float(x[wr_key]), -int(x[games_key]), str(x["username"]).casefold()))
            title = f"{resolved} — {label}"
            lines = [f"**{i}.** `{x['username']}` — **{x[wr_key]:.1f}% WR** • {x[wins_key]}W–{x[losses_key]}L • {x[games_key]} games" for i, x in enumerate(items, 1)]
        elif value == "rivals":
            items = [x for x in items if int(x["opposite_games"]) >= minimum]
            items.sort(key=lambda x: (-int(x["opposite_games"]), -float(x["opponent_win_rate"]), str(x["username"]).casefold()))
            title = f"{resolved} — Rivals"
            lines = [f"**{i}.** `{x['username']}` — **{x['opposite_games']} opposite** • {x['opponent_wins']}W–{x['opponent_losses']}L • {x['opponent_win_rate']:.1f}% WR" for i, x in enumerate(items, 1)]
        else:
            items = [x for x in items if int(x["opposite_games"]) >= minimum]
            items.sort(key=lambda x: (-float(x["opponent_win_rate"]), -int(x["opponent_games"] if "opponent_games" in x else x["opposite_games"]), str(x["username"]).casefold()))
            title = f"{resolved} — Most Dominated"
            lines = [f"**{i}.** `{x['username']}` — **{x['opponent_win_rate']:.1f}% WR** • {x['opponent_wins']}W–{x['opponent_losses']}L • {x['opposite_games']} games" for i, x in enumerate(items, 1)]

        view = SocialListView(
            title=title,
            lines=lines,
            season_name=season_display_name(season),
            minimum_games=minimum,
            page_size=int(results_per_page),
            requesting_user_id=interaction.user.id,
        )
        await interaction.response.send_message(embed=view.build_embed(), view=view)

    @app_commands.command(name="info", description="Shows general information about a previous season.")
    @app_commands.describe(season_prefix="Season prefix, for example s4 or r1.")
    async def info(self, interaction: discord.Interaction, season_prefix: str):
        season = await self._season(interaction, season_prefix)
        if not season:
            return await interaction.response.send_message("That season prefix was not found.", ephemeral=True)

        rows = await season_rows(season["id"])
        names = {p["username"].casefold(): p["username"] for row in rows for p in unpack(row)}
        summary = season_overview_summary(rows)
        status = "Active" if season["is_active"] else "Archived"

        e = discord.Embed(
            title=f"{season_display_name(season)} — Season Info",
            color=discord.Color.blurple(),
        )
        e.add_field(name="Status", value=status, inline=True)
        e.add_field(name="Matches", value=str(len(rows)), inline=True)
        e.add_field(name="Players Seen", value=str(len(names)), inline=True)
        result_lines = [
            (
                f"**Resistance:** {summary['resistance_wins']:,} wins • "
                f"**{summary['resistance_win_rate']:.1f}%**"
            ),
            (
                f"**Spies:** {summary['spy_wins']:,} wins • "
                f"**{summary['spy_win_rate']:.1f}%**"
            ),
        ]
        if summary["unknown_results"]:
            result_lines.append(
                f"Unknown/unresolved results: **{summary['unknown_results']:,}**"
            )
        e.add_field(
            name="Season Results",
            value="\n".join(result_lines),
            inline=False,
        )

        def timed_match_line(label, seconds, match_id):
            if seconds is None:
                return f"**{label}:** N/A"
            match_suffix = f" • Match #{match_id}" if match_id is not None else ""
            return f"**{label}:** {fmt_duration(seconds)}{match_suffix}"

        e.add_field(
            name="Game Lengths",
            value=(
                f"**Average:** {fmt_duration(summary['average_duration_seconds'])}\n"
                f"**Median:** {fmt_duration(summary['median_duration_seconds'])}\n"
                + timed_match_line(
                    "Shortest",
                    summary["shortest_duration_seconds"],
                    summary["shortest_match_id"],
                )
                + "\n"
                + timed_match_line(
                    "Longest",
                    summary["longest_duration_seconds"],
                    summary["longest_match_id"],
                )
                + "\n"
                f"**Total recorded play time:** "
                f"{fmt_duration(summary['total_duration_seconds'])}\n"
                f"Duration data: **{summary['timed_matches']:,}/{summary['matches']:,} matches**"
            ),
            inline=False,
        )

        assassination_text = (
            f"{fmt_duration(summary['average_assassination_seconds'])} "
            f"({summary['timed_assassinations']:,} timed)"
            if summary["average_assassination_seconds"] is not None
            else "N/A"
        )
        youtube_rate = (
            summary["youtube_matches"] / summary["matches"] * 100
            if summary["matches"]
            else 0.0
        )
        e.add_field(
            name="Additional Season Data",
            value=(
                f"**Average missions:** "
                f"{summary['average_missions']:.2f}\n"
                if summary["average_missions"] is not None
                else "**Average missions:** N/A\n"
            )
            + (
                f"**Average assassination phase:** {assassination_text}\n"
                f"**YouTube recordings:** {summary['youtube_matches']:,}/"
                f"{summary['matches']:,} ({youtube_rate:.1f}%)"
            ),
            inline=False,
        )
        e.add_field(name="Started", value=fmt_date(season["started_at"]), inline=True)
        e.add_field(name="Ended", value=fmt_date(season["ended_at"]) if season["ended_at"] else "Still active", inline=True)
        await interaction.response.send_message(embed=e)

    @app_commands.command(
        name="stats",
        description="Shows detailed outcome, hammer, and card stats for a past season.",
    )
    @app_commands.describe(season_prefix="Season prefix, for example s4.")
    async def advanced_stats(
        self,
        interaction: discord.Interaction,
        season_prefix: str,
    ):
        season = await self._season(interaction, season_prefix)
        if not season:
            return await interaction.response.send_message(
                "That season prefix was not found.", ephemeral=True
            )
        summary = season_overview_summary(await season_rows(int(season["id"])))
        embed = discord.Embed(
            title=f"{season_display_name(season)} — Detailed Statistics",
            description=f"Based on **{summary['matches']:,}** archived matches.",
            color=discord.Color.blurple(),
        )
        m1_total = summary["m1_passes"] + summary["m1_fails"]
        embed.add_field(
            name="Mission 1 Outcomes",
            value=(
                f"**Passes:** {summary['m1_passes']:,} "
                f"({summary['m1_passes'] / m1_total * 100 if m1_total else 0:.1f}%)\n"
                f"↳ {format_outcome_split(summary['m1_outcomes']['Pass'])}\n"
                f"**Fails:** {summary['m1_fails']:,} "
                f"({summary['m1_fails'] / m1_total * 100 if m1_total else 0:.1f}%)\n"
                f"↳ {format_outcome_split(summary['m1_outcomes']['Fail'])}"
            ),
            inline=False,
        )
        hammer_lines = [
            f"**Resistance hammer:** "
            f"{format_outcome_split(summary['m1_hammer_outcomes']['Resistance'])}",
            f"**Spy hammer:** "
            f"{format_outcome_split(summary['m1_hammer_outcomes']['Spy'])}",
        ]
        if summary["m1_hammer"]["Unknown"]:
            hammer_lines.append(
                f"**Unknown hammer:** "
                f"{format_outcome_split(summary['m1_hammer_outcomes']['Unknown'])}"
            )
        hammer_coverage = sum(summary["m1_hammer"].values())
        hammer_lines.append(
            f"**Coverage:** {hammer_coverage:,}/{summary['matches']:,} matches"
        )
        embed.add_field(
            name="Mission 1 Hammer",
            value="\n".join(hammer_lines),
            inline=False,
        )
        start_lines = [
            f"**Starts with Resistance:** "
            f"{format_outcome_split(summary['card_start_outcomes']['Resistance'])}",
            f"**Starts with Spy:** "
            f"{format_outcome_split(summary['card_start_outcomes']['Spy'])}",
        ]
        if summary["card_starts"]["Unknown"]:
            start_lines.append(
                f"**Unknown starting holder:** "
                f"{format_outcome_split(summary['card_start_outcomes']['Unknown'])}"
            )
        embed.add_field(
            name="Ref of the Rain — Starting Holder",
            value="\n".join(start_lines),
            inline=False,
        )
        unused_lines = [
            f"**Resistance held, never used:** "
            f"{format_outcome_split(summary['card_unused_outcomes']['Resistance'])}",
            f"**Spy held, never used:** "
            f"{format_outcome_split(summary['card_unused_outcomes']['Spy'])}",
        ]
        if summary["card_unused_outcomes"]["Unknown"][0]:
            unused_lines.append(
                f"**Unknown holder, never used:** "
                f"{format_outcome_split(summary['card_unused_outcomes']['Unknown'])}"
            )
        embed.add_field(
            name="Ref of the Rain — Never Used",
            value="\n".join(unused_lines),
            inline=False,
        )
        transition_lines = []
        for label, (games, spy_wins) in summary["card_transitions"].items():
            transition_lines.append(
                f"**{label}:** "
                f"{format_outcome_split([games, games - spy_wins, spy_wins])}"
            )
        embed.add_field(
            name="Ref of the Rain — First Handoff",
            value=(
                "\n".join(transition_lines)
                + f"\n**Handoff coverage:** "
                f"{sum(item[0] for item in summary['card_transitions'].values()):,}/"
                f"{summary['matches']:,} matches"
            ),
            inline=False,
        )
        stopped_lines = [
            f"**{label}:** {format_outcome_split(values)}"
            for label, values in summary["card_first_handoff_stops"].items()
        ]
        embed.add_field(
            name="Ref of the Rain — Handoff Then Stopped",
            value="\n".join(stopped_lines),
            inline=False,
        )
        embed.add_field(
            name="Ref of the Rain — Handoff Count Summary",
            value=(
                f"**Exactly zero handoffs:** "
                f"{format_outcome_split(summary['card_exact_handoff_outcomes']['Zero'])}\n"
                f"**Exactly one handoff:** "
                f"{format_outcome_split(summary['card_exact_handoff_outcomes']['One'])}\n"
                f"**Exactly two handoffs:** "
                f"{format_outcome_split(summary['card_exact_handoff_outcomes']['Two'])}"
            ),
            inline=False,
        )
        chain_lines = []
        for label, (games, spy_wins) in summary["card_three_chains"].items():
            if not games:
                continue
            chain_lines.append(
                f"**{label}:** "
                f"{format_outcome_split([games, games - spy_wins, spy_wins])}"
            )
        embed.add_field(
            name="Ref of the Rain — First Three Holders",
            value=(
                (
                    "\n".join(chain_lines) if chain_lines
                    else "No matches recorded a third holder."
                )
                + f"\n**Three-holder coverage:** "
                f"{sum(item[0] for item in summary['card_three_chains'].values()):,}/"
                f"{summary['matches']:,} matches"
            ),
            inline=False,
        )
        embed.set_footer(
            text="Recovered archive data is included automatically."
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(
        name="merlin-shots",
        description="Shows a player's Merlin-target history in a past season.",
    )
    @app_commands.describe(
        season_prefix="Season prefix, for example s4.",
        username="Player name. Leave blank to use your linked account.",
        discord_user="Linked Discord user to inspect.",
    )
    async def merlin_shots(
        self,
        interaction: discord.Interaction,
        season_prefix: str,
        username: str | None = None,
        discord_user: discord.Member | None = None,
    ):
        season = await self._season(interaction, season_prefix)
        if not season:
            return await interaction.response.send_message(
                "That season prefix was not found.", ephemeral=True
            )
        resolved, error = await self._player(
            interaction, season, username, discord_user
        )
        if error:
            return await interaction.response.send_message(error, ephemeral=True)
        stats = await get_player_merlin_shot_stats(int(season["id"]), resolved)
        lines = []
        for role in ROLE_BUCKETS:
            games = stats.games_by_role[role]
            shots = stats.shots_by_role[role]
            rate = shots / games * 100 if games else 0.0
            lines.append(
                f"**{role}:** {shots:,} shots / {games:,} games ({rate:.1f}%)"
            )
        false_rate = (
            stats.false_merlin_shots / stats.non_merlin_games * 100
            if stats.non_merlin_games else 0.0
        )
        embed = discord.Embed(
            title=(
                f"{resolved} — {season_display_name(season)} Merlin Shots"
            ),
            description=(
                "\n".join(lines)
                + "\n\n"
                + f"**False Merlin shots:** {stats.false_merlin_shots:,} / "
                f"{stats.non_merlin_games:,} non-Merlin Resistance games "
                f"({false_rate:.1f}%)\n"
                + f"**All Merlin-target selections:** {stats.total_shots:,}"
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="Recovered archive data is included automatically.")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="recent", description="Shows recent matches from a previous season.")
    @app_commands.describe(season_prefix="Season prefix, for example s4 or r1.")
    async def recent(self, interaction: discord.Interaction, season_prefix: str, count: app_commands.Range[int,1,15]=5):
        season = await self._season(interaction, season_prefix)
        if not season:
            return await interaction.response.send_message("That season prefix was not found.", ephemeral=True)
        rows = await recent_rows(season["id"], count)
        if not rows:
            return await interaction.response.send_message("That season has no archived matches.", ephemeral=True)

        e = discord.Embed(title=f"{season_display_name(season)} — Recent Matches", color=discord.Color.blurple())
        for row in rows:
            players = json.loads(row["players_json"])
            recording = (
                f"\n[Watch recording]({row['youtube_url']})"
                if row["youtube_url"] else ""
            )
            e.add_field(
                name=f"Match #{row['season_match_id']} • {fmt_date(row['finished_at'])}",
                value=f"**{row['winner'] or 'Unknown'} Victory**\n" + ", ".join(players) + recording,
                inline=False,
            )
        await interaction.response.send_message(embed=e)

    @app_commands.command(name="match", description="Shows one match from a previous season.")
    @app_commands.describe(season_prefix="Season prefix, for example s4 or r1.", match_id="Season-local match ID.")
    async def match(self, interaction: discord.Interaction, season_prefix: str, match_id: app_commands.Range[int,1,None]):
        season = await self._season(interaction, season_prefix)
        if not season:
            return await interaction.response.send_message("That season prefix was not found.", ephemeral=True)
        row = await find_match(season["id"], int(match_id))
        if not row:
            return await interaction.response.send_message("That match ID was not found in that season.", ephemeral=True)

        parts = unpack(row)
        res = [p for p in parts if p["alignment"] == "Resistance"]
        spies = [p for p in parts if p["alignment"] == "Spy"]
        e = discord.Embed(
            title=f"{season_display_name(season)} — Match #{row['season_match_id']}",
            description=f"**{row['winner'] or 'Unknown'} Victory** • {fmt_date(row['finished_at'])}",
            color=discord.Color.blurple(),
        )
        e.add_field(name="Resistance", value="\n".join(f"`{p['username']}` — {p['role'] or 'Unknown'}" for p in res) or "N/A", inline=False)
        e.add_field(name="Spies", value="\n".join(f"`{p['username']}` — {p['role'] or 'Unknown'}" for p in spies) or "N/A", inline=False)
        missions = json.loads(row["mission_history_json"])
        e.add_field(name="Missions", value=" ".join("✅" if str(x).casefold()=="succeeded" else "❌" for x in missions) or "N/A", inline=False)
        if row["youtube_url"]:
            e.add_field(name="Recording", value=f"[Watch the full match on YouTube]({row['youtube_url']})", inline=False)
        await interaction.response.send_message(embed=e)

    @app_commands.command(name="leaderboard", description="Shows an archived leaderboard for a previous season.")
    @app_commands.describe(
        season_prefix="Season prefix, for example s4 or r1.",
        statistic="Archived statistic to rank.",
        minimum_games="Minimum qualifying games.",
    )
    @app_commands.choices(statistic=[
        app_commands.Choice(name="Games Played", value="games"),
        app_commands.Choice(name="Overall WR", value="overall"),
        app_commands.Choice(name="Resistance WR", value="res"),
        app_commands.Choice(name="Spy WR", value="spy"),
    ])
    async def leaderboard(
        self,
        interaction: discord.Interaction,
        season_prefix: str,
        statistic: app_commands.Choice[str],
        minimum_games: app_commands.Range[int,1,999]=5,
    ):
        season = await self._season(interaction, season_prefix)
        if not season:
            return await interaction.response.send_message("That season prefix was not found.", ephemeral=True)
        rows = await season_rows(season["id"])
        names = sorted({p["username"] for row in rows for p in unpack(row)}, key=str.casefold)
        data = []
        for username in names:
            overall = player_summary(rows, username)
            aligns = alignment_summary(rows, username)
            if statistic.value == "games":
                games, value = overall["games"], overall["games"]
            elif statistic.value == "res":
                games, value = aligns["Resistance"]["games"], aligns["Resistance"]["wr"]
            elif statistic.value == "spy":
                games, value = aligns["Spy"]["games"], aligns["Spy"]["wr"]
            else:
                games, value = overall["games"], overall["wr"]
            if games >= minimum_games:
                data.append((username, games, value))

        if statistic.value == "games":
            data.sort(key=lambda x: (-x[2], x[0].casefold()))
        else:
            data.sort(key=lambda x: (-x[2], -x[1], x[0].casefold()))
        if not data:
            return await interaction.response.send_message("No players meet that requirement.", ephemeral=True)

        label = {"games":"Games Played","overall":"Overall WR","res":"Resistance WR","spy":"Spy WR"}[statistic.value]
        lines = []
        for i,(username,games,value) in enumerate(data[:25],1):
            metric = f"{int(value)} GP" if statistic.value=="games" else f"{value:.1f}% WR • {games} GP"
            lines.append(f"**{i}.** `{username}` — {metric}")
        e = discord.Embed(
            title=f"{season_display_name(season)} — {label}",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=e)

    @app_commands.command(name="records", description="Shows statistical records from a previous season.")
    @app_commands.describe(season_prefix="Season prefix, for example s4 or r1.", minimum_games="Minimum games for rate records.")
    async def records(self, interaction: discord.Interaction, season_prefix: str, minimum_games: app_commands.Range[int,1,999]=5):
        season = await self._season(interaction, season_prefix)
        if not season:
            return await interaction.response.send_message("That season prefix was not found.", ephemeral=True)
        rows = await season_rows(season["id"])
        x = season_records(rows, minimum_games)
        if not rows:
            return await interaction.response.send_message("That season has no archived matches.", ephemeral=True)

        def line(v, alignment_key=None):
            if not v:
                return "N/A"
            if alignment_key:
                a=v[alignment_key]
                return f"`{v['username']}` — {a['wr']:.1f}% WR • {a['games']} GP"
            return f"`{v['username']}` — {v['wr']:.1f}% WR • {v['games']} GP"

        e=discord.Embed(title=f"{season_display_name(season)} — Records", color=discord.Color.blurple())
        e.add_field(name="Most Games", value=(f"`{x['most_games']['username']}` — {x['most_games']['games']} GP" if x["most_games"] else "N/A"), inline=False)
        e.add_field(name="Best Overall WR", value=line(x["best_wr"]), inline=False)
        e.add_field(name="Worst Overall WR", value=line(x["worst_wr"]), inline=False)
        e.add_field(name="Best Resistance WR", value=line(x["best_res"],"res"), inline=False)
        e.add_field(name="Best Spy WR", value=line(x["best_spy"],"spy"), inline=False)
        await interaction.response.send_message(embed=e)


async def setup(bot:commands.Bot):
    await bot.add_cog(Insights(bot))
    await bot.add_cog(PastSeason(bot))
