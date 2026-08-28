from __future__ import annotations

from datetime import datetime, timezone
import json
import discord
from discord import app_commands
from discord.ext import commands
from database.insights import active_or_latest,season_rows,recent_rows,find_match,registered_usernames,all_links_for_discord,bot_status_data,unpack,player_summary,streak_summary,role_summary,alignment_summary,role_board,activity_summary,season_records,ratings_for_usernames,relationships
from utils.permissions import bot_admin_only
from utils.discord_users import parse_discord_user_id
from database.seasons import get_season_by_prefix

def fmt_date(v):
    if not v: return 'N/A'
    try: return datetime.fromisoformat(str(v).replace('Z','+00:00')).strftime('%b %d, %Y')
    except Exception: return str(v)[:10]

async def player_auto(interaction,current):
    if interaction.guild_id is None: return []
    s=await active_or_latest(interaction.guild_id)
    if not s: return []
    vals=await registered_usernames(s['id']); q=current.casefold()
    return [app_commands.Choice(name=v,value=v) for v in vals if q in v.casefold()][:25]

async def resolve_name(interaction,username):
    if interaction.guild_id is None: return None,None
    s=await active_or_latest(interaction.guild_id)
    if not s: return None,None
    vals=await registered_usernames(s['id'])
    hit=next((v for v in vals if v.casefold()==username.strip().casefold()),username.strip())
    return s,hit

class Insights(commands.Cog):
    def __init__(self,bot): self.bot=bot

    @app_commands.command(name='recent',description='Shows the most recent archived season matches.')
    async def recent(self,interaction:discord.Interaction,count:app_commands.Range[int,1,15]=5):
        s=await active_or_latest(interaction.guild_id)
        if not s: return await interaction.response.send_message('No season history exists.',ephemeral=True)
        rows=await recent_rows(s['id'],count)
        if not rows: return await interaction.response.send_message('No archived matches yet.',ephemeral=True)
        e=discord.Embed(title=f"{s['required_prefix']} — Recent Matches",color=discord.Color.blurple())
        for r in rows:
            players=json.loads(r['players_json']); e.add_field(name=f"Match #{r['season_match_id']} • {fmt_date(r['finished_at'])}",value=f"**{r['winner'] or 'Unknown'} Victory**\n"+', '.join(players),inline=False)
        await interaction.response.send_message(embed=e)

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
                1 for result in missions
                if str(result).casefold() == "succeeded"
            )
            failed = sum(
                1 for result in missions
                if str(result).casefold() == "failed"
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
                elapsed = max(0, int((now - started_at).total_seconds()))
                hours, remainder = divmod(elapsed, 3600)
                minutes, seconds = divmod(remainder, 60)
                if hours:
                    elapsed_text = f"{hours}h {minutes}m {seconds}s"
                elif minutes:
                    elapsed_text = f"{minutes}m {seconds}s"
                else:
                    elapsed_text = f"{seconds}s"
                embed.add_field(
                    name="Game Time",
                    value=f"**{elapsed_text}**",
                    inline=True,
                )

            game_mode = game.get("gameMode")
            if game_mode:
                embed.add_field(
                    name="Mode",
                    value=str(game_mode),
                    inline=True,
                )

            embed.add_field(
                name="Score",
                value=f"Resistance **{succeeded}** — **{failed}** Spies",
                inline=True,
            )

            mission_parts = []
            for i, result in enumerate(missions):
                icon = (
                    "✅" if str(result).casefold() == "succeeded"
                    else "❌" if str(result).casefold() == "failed"
                    else "➖"
                )
                fail_text = ""
                if i < len(fails):
                    try:
                        count = int(fails[i])
                    except (TypeError, ValueError):
                        count = 0
                    if count > 0:
                        fail_text = f" ({count}F)"
                mission_parts.append(f"M{i + 1} {icon}{fail_text}")

            if succeeded < 3 and failed < 3:
                current_mission = min(5, len(missions) + 1)
                mission_parts.append(f"**Current: M{current_mission}**")

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

            # Public card data only; never expose hidden role information.
            public_data = data.get("publicData") or {}
            public_cards = public_data.get("cards") or {}
            ref_history = []
            if isinstance(public_cards, dict):
                for card_key, card_data in public_cards.items():
                    if not isinstance(card_data, dict):
                        continue
                    card_name = str(card_data.get("name") or card_key or "")
                    normalized = (
                        card_name.replace("_", " ")
                        .replace("-", " ")
                        .casefold()
                    )
                    if "ref" in normalized and "rain" in normalized:
                        ref_history = [
                            str(name)
                            for name in (card_data.get("history") or [])
                            if isinstance(name, str) and name.strip()
                        ]
                        break

            if ref_history:
                embed.add_field(
                    name="Ref of the Rain",
                    value=(
                        f"**Current:** `{ref_history[-1]}`\n"
                        + " → ".join(f"`{name}`" for name in ref_history)
                    )[:1024],
                    inline=False,
                )

            embed.set_footer(
                text="Live public snapshot • no hidden roles are shown"
            )
            embeds.append(embed)

        await interaction.followup.send(embeds=embeds[:10])

    @app_commands.command(name='streaks',description='Shows current and longest win/loss streaks.')
    @app_commands.autocomplete(username=player_auto)
    async def streaks(self,interaction:discord.Interaction,username:str):
        s,u=await resolve_name(interaction,username); rows=await season_rows(s['id']) if s else []; x=streak_summary(rows,u) if s else {'games':0}
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
            title=f"{s['required_prefix']} — Records",
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
    @app_commands.autocomplete(username=player_auto)
    async def role_stats(self,interaction:discord.Interaction,username:str):
        s,u=await resolve_name(interaction,username); data=role_summary(await season_rows(s['id']),u) if s else []
        if not data: return await interaction.response.send_message('No archived role data found.',ephemeral=True)
        await interaction.response.send_message(embed=discord.Embed(title=f'{u} — Role Stats',color=discord.Color.blurple(),description='\n'.join(f"**{x['role']}** — {x['wins']}W-{x['losses']}L • {x['wr']:.1f}% • {x['games']} GP" for x in data)))

    @app_commands.command(name='role-leaderboard',description='Ranks season players for one role.')
    async def role_leaderboard(self,interaction:discord.Interaction,role:str,minimum_games:app_commands.Range[int,1,999]=3):
        s=await active_or_latest(interaction.guild_id); data=role_board(await season_rows(s['id']),role) if s else []; data=[x for x in data if x['games']>=minimum_games]; data.sort(key=lambda x:(-x['wr'],-x['games'],x['username'].casefold()))
        if not data: return await interaction.response.send_message('No qualifying role data found.',ephemeral=True)
        lines=[f"{i}. `{x['username']}` — {x['games']} GP • {x['wr']:.1f}% WR" for i,x in enumerate(data[:25],1)]
        await interaction.response.send_message(embed=discord.Embed(title=f"{s['required_prefix']} — {role.title()} Leaderboard",description='\n'.join(lines),color=discord.Color.blurple()))

    @app_commands.command(name='alignment-stats',description='Shows a player’s Resistance vs Spy performance.')
    @app_commands.autocomplete(username=player_auto)
    async def alignment_stats(self,interaction:discord.Interaction,username:str):
        s,u=await resolve_name(interaction,username); x=alignment_summary(await season_rows(s['id']),u) if s else {}
        e=discord.Embed(title=f'{u} — Alignment Stats',color=discord.Color.blurple())
        for k in ('Resistance','Spy'):
            v=x.get(k,{'games':0,'wins':0,'losses':0,'wr':0}); e.add_field(name=k,value=f"{v['wins']}W-{v['losses']}L • {v['wr']:.1f}% WR • {v['games']} GP",inline=False)
        await interaction.response.send_message(embed=e)

    @app_commands.command(name='activity',description='Shows recent season activity for a player.')
    @app_commands.describe(days='Number of recent days to analyze. Defaults to 7.')
    @app_commands.autocomplete(username=player_auto)
    async def activity(self,interaction:discord.Interaction,username:str,days:app_commands.Range[int,1,365]=7):
        s,u=await resolve_name(interaction,username); x=activity_summary(await season_rows(s['id']),u,days) if s else None
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
            rows=await season_rows(l['community_season_id']) if l['community_season_id'] else []; x=player_summary(rows,l['proavalon_username']); lines.append(f"**{l['required_prefix'] or 'General'}** — `{l['proavalon_username']}` • {x['games']} GP • {x['wr']:.1f}% WR")
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
        e.add_field(name='Active Season',value='None' if not s else f"`{s['required_prefix']}` • ID {s['id']}",inline=False)
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

    @app_commands.command(name="info", description="Shows general information about a previous season.")
    @app_commands.describe(season_prefix="Season prefix, for example s4 or r1.")
    async def info(self, interaction: discord.Interaction, season_prefix: str):
        season = await self._season(interaction, season_prefix)
        if not season:
            return await interaction.response.send_message("That season prefix was not found.", ephemeral=True)

        rows = await season_rows(season["id"])
        names = {p["username"].casefold(): p["username"] for row in rows for p in unpack(row)}
        res_wins = sum(1 for row in rows if str(row["winner"] or "").casefold().startswith("res"))
        spy_wins = sum(1 for row in rows if str(row["winner"] or "").casefold().startswith("spy"))
        status = "Active" if season["is_active"] else "Archived"

        e = discord.Embed(
            title=f"{season['required_prefix']} — Season Info",
            color=discord.Color.blurple(),
        )
        e.add_field(name="Status", value=status, inline=True)
        e.add_field(name="Matches", value=str(len(rows)), inline=True)
        e.add_field(name="Players Seen", value=str(len(names)), inline=True)
        e.add_field(name="Resistance Wins", value=str(res_wins), inline=True)
        e.add_field(name="Spy Wins", value=str(spy_wins), inline=True)
        e.add_field(name="Started", value=fmt_date(season["started_at"]), inline=True)
        e.add_field(name="Ended", value=fmt_date(season["ended_at"]) if season["ended_at"] else "Still active", inline=True)
        await interaction.response.send_message(embed=e)

    @app_commands.command(name="recent", description="Shows recent matches from a previous season.")
    @app_commands.describe(season_prefix="Season prefix, for example s4 or r1.")
    async def recent(self, interaction: discord.Interaction, season_prefix: str, count: app_commands.Range[int,1,15]=5):
        season = await self._season(interaction, season_prefix)
        if not season:
            return await interaction.response.send_message("That season prefix was not found.", ephemeral=True)
        rows = await recent_rows(season["id"], count)
        if not rows:
            return await interaction.response.send_message("That season has no archived matches.", ephemeral=True)

        e = discord.Embed(title=f"{season['required_prefix']} — Recent Matches", color=discord.Color.blurple())
        for row in rows:
            players = json.loads(row["players_json"])
            e.add_field(
                name=f"Match #{row['season_match_id']} • {fmt_date(row['finished_at'])}",
                value=f"**{row['winner'] or 'Unknown'} Victory**\n" + ", ".join(players),
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
            title=f"{season['required_prefix']} — Match #{row['season_match_id']}",
            description=f"**{row['winner'] or 'Unknown'} Victory** • {fmt_date(row['finished_at'])}",
            color=discord.Color.blurple(),
        )
        e.add_field(name="Resistance", value="\n".join(f"`{p['username']}` — {p['role'] or 'Unknown'}" for p in res) or "N/A", inline=False)
        e.add_field(name="Spies", value="\n".join(f"`{p['username']}` — {p['role'] or 'Unknown'}" for p in spies) or "N/A", inline=False)
        missions = json.loads(row["mission_history_json"])
        e.add_field(name="Missions", value=" ".join("✅" if str(x).casefold()=="succeeded" else "❌" for x in missions) or "N/A", inline=False)
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
            title=f"{season['required_prefix']} — {label}",
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

        e=discord.Embed(title=f"{season['required_prefix']} — Records", color=discord.Color.blurple())
        e.add_field(name="Most Games", value=(f"`{x['most_games']['username']}` — {x['most_games']['games']} GP" if x["most_games"] else "N/A"), inline=False)
        e.add_field(name="Best Overall WR", value=line(x["best_wr"]), inline=False)
        e.add_field(name="Worst Overall WR", value=line(x["worst_wr"]), inline=False)
        e.add_field(name="Best Resistance WR", value=line(x["best_res"],"res"), inline=False)
        e.add_field(name="Best Spy WR", value=line(x["best_spy"],"spy"), inline=False)
        await interaction.response.send_message(embed=e)


async def setup(bot:commands.Bot):
    await bot.add_cog(Insights(bot))
    await bot.add_cog(PastSeason(bot))