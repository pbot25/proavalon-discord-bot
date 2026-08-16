from __future__ import annotations

from datetime import datetime
import json
import discord
from discord import app_commands
from discord.ext import commands
from database.insights import active_or_latest,season_rows,recent_rows,find_match,registered_usernames,all_links_for_discord,bot_status_data,unpack,player_summary,streak_summary,role_summary,alignment_summary,role_board,activity_summary,relationships,season_records,ratings_for_usernames
from utils.permissions import bot_admin_only
from utils.discord_users import parse_discord_user_id

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
            players=json.loads(r['players_json']); e.add_field(name=f"Match #{r['id']} • Room {r['room_id'] or 'N/A'}",value=f"**{r['winner'] or 'Unknown'} Victory** • {fmt_date(r['finished_at'])}\n"+', '.join(players),inline=False)
        await interaction.response.send_message(embed=e)

    @app_commands.command(name='match',description='Shows one archived match by match ID or room ID.')
    async def match(self,interaction:discord.Interaction,match_id:int):
        s=await active_or_latest(interaction.guild_id)
        if not s: return await interaction.response.send_message('No season history exists.',ephemeral=True)
        r=await find_match(s['id'],match_id)
        if not r: return await interaction.response.send_message('That match was not found in this season.',ephemeral=True)
        parts=unpack(r); res=[p for p in parts if p['alignment']=='Resistance']; spies=[p for p in parts if p['alignment']=='Spy']
        e=discord.Embed(title=f"Match #{r['id']} — {r['winner'] or 'Unknown'} Victory",description=f"Room {r['room_id'] or 'N/A'} • {fmt_date(r['finished_at'])}",color=discord.Color.blurple())
        e.add_field(name='Resistance',value='\n'.join(f"`{p['username']}` — {p['role'] or 'Unknown'}" for p in res) or 'N/A',inline=False)
        e.add_field(name='Spies',value='\n'.join(f"`{p['username']}` — {p['role'] or 'Unknown'}" for p in spies) or 'N/A',inline=False)
        missions=json.loads(r['mission_history_json']); e.add_field(name='Missions',value=' '.join('✅' if str(x).casefold()=='succeeded' else '❌' for x in missions) or 'N/A',inline=False)
        await interaction.response.send_message(embed=e)

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

    @app_commands.command(name='rivals',description='Shows players most often opposite your alignment.')
    @app_commands.autocomplete(username=player_auto)
    async def rivals(self,interaction:discord.Interaction,username:str,minimum_shared_games:app_commands.Range[int,1,999]=5):
        s,u=await resolve_name(interaction,username); data=relationships(await season_rows(s['id']),u) if s else []; data=[x for x in data if x['shared']>=minimum_shared_games]; data.sort(key=lambda x:(-x['opp_pct'],-x['shared']))
        lines=[f"{i}. `{x['username']}` — **{x['opp_pct']:.1f}%** opposite ({x['opp']}/{x['shared']}) • H2H {x['opp_wins']}-{x['opp_losses']}" for i,x in enumerate(data[:10],1)]
        await interaction.response.send_message(embed=discord.Embed(title=f'{u} — Rivals',description='\n'.join(lines) or 'Not enough tracked games yet.',color=discord.Color.blurple()))

    @app_commands.command(name='teammates',description='Shows players most often on your alignment.')
    @app_commands.autocomplete(username=player_auto)
    async def teammates(self,interaction:discord.Interaction,username:str,minimum_shared_games:app_commands.Range[int,1,999]=5):
        s,u=await resolve_name(interaction,username); data=relationships(await season_rows(s['id']),u) if s else []; data=[x for x in data if x['shared']>=minimum_shared_games]; data.sort(key=lambda x:(-x['same_pct'],-x['shared']))
        lines=[f"{i}. `{x['username']}` — **{x['same_pct']:.1f}%** same ({x['same']}/{x['shared']})" for i,x in enumerate(data[:10],1)]
        await interaction.response.send_message(embed=discord.Embed(title=f'{u} — Teammates',description='\n'.join(lines) or 'Not enough tracked games yet.',color=discord.Color.blurple()))

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
        await interaction.response.send_message(embed=e,ephemeral=True)

async def setup(bot:commands.Bot): await bot.add_cog(Insights(bot))