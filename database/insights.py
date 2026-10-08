from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from database.connection import connect

RES_ROLES={"resistance","tristan","isolde","percival","merlin","melron","moregano"}
SPY_ROLES={"spy","assassin","morgana","mordred","oberon","hitberon","mordredassassin"}

def _j(v, default):
    if isinstance(v,(list,dict)): return v
    try: return json.loads(v) if v is not None else default
    except Exception: return default

def alignment(role):
    if not role: return None
    k=str(role).replace(' ','').casefold()
    if k in RES_ROLES: return 'Resistance'
    if k in SPY_ROLES: return 'Spy'
    return None

def winner_norm(w):
    if not w: return None
    k=str(w).casefold()
    if 'res' in k: return 'Resistance'
    if 'spy' in k: return 'Spy'
    return None

async def active_or_latest(guild_id:int):
    db=await connect()
    try:
        cur=await db.execute("SELECT * FROM community_seasons WHERE guild_id=? ORDER BY is_active DESC,id DESC LIMIT 1",(str(guild_id),))
        return await cur.fetchone()
    finally: await db.close()

async def season_rows(season_id:int):
    db=await connect()
    try:
        cur=await db.execute("SELECT * FROM season_matches WHERE community_season_id=? ORDER BY finished_at ASC,id ASC",(season_id,))
        return await cur.fetchall()
    finally: await db.close()

async def recent_rows(season_id:int, limit:int):
    db=await connect()
    try:
        cur=await db.execute("SELECT * FROM season_matches WHERE community_season_id=? ORDER BY finished_at DESC,id DESC LIMIT ?",(season_id,limit))
        return await cur.fetchall()
    finally: await db.close()

async def season_match_player_names(season_id: int) -> list[str]:
    """Return the distinct archived player names for a season."""
    db = await connect()
    try:
        cur = await db.execute(
            """
            SELECT MIN(CAST(player.value AS TEXT)) AS username
            FROM season_matches AS sm
            JOIN json_each(sm.players_json) AS player
            WHERE sm.community_season_id = ?
              AND TRIM(CAST(player.value AS TEXT)) <> ''
            GROUP BY LOWER(TRIM(CAST(player.value AS TEXT)))
            ORDER BY username COLLATE NOCASE
            """,
            (season_id,),
        )
        return [str(row["username"]) for row in await cur.fetchall()]
    finally:
        await db.close()

async def season_player_discord_links(season_id: int):
    """Return active Discord links scoped to the selected season."""
    db = await connect()
    try:
        cur = await db.execute(
            """
            SELECT links.proavalon_username, links.discord_user_id
            FROM account_links AS links
            JOIN community_seasons AS season
              ON season.guild_id = links.guild_id
             AND season.season_number = links.season_number
            WHERE season.id = ?
              AND links.is_active = 1
            """,
            (season_id,),
        )
        return await cur.fetchall()
    finally:
        await db.close()

async def search_matches_by_players(season_id: int, usernames: list[str]):
    """Find newest matches containing every supplied player name."""
    keys = list(dict.fromkeys(
        name.strip().casefold() for name in usernames if name.strip()
    ))
    if not keys:
        return []

    placeholders = ",".join("?" for _ in keys)
    db = await connect()
    try:
        cur = await db.execute(
            f"""
            SELECT sm.*
            FROM season_matches AS sm
            JOIN json_each(sm.players_json) AS player
            WHERE sm.community_season_id = ?
              AND LOWER(TRIM(CAST(player.value AS TEXT))) IN ({placeholders})
            GROUP BY sm.id
            HAVING COUNT(DISTINCT LOWER(TRIM(CAST(player.value AS TEXT)))) = ?
            ORDER BY sm.finished_at DESC, sm.id DESC
            """,
            (season_id, *keys, len(keys)),
        )
        return await cur.fetchall()
    finally:
        await db.close()

async def find_match(season_id:int, match_id:int):
    """Find a match only by its season-local public match ID."""
    db=await connect()
    try:
        cur=await db.execute(
            """
            SELECT *
            FROM season_matches
            WHERE community_season_id=?
              AND season_match_id=?
            LIMIT 1
            """,
            (season_id,match_id),
        )
        return await cur.fetchone()
    finally: await db.close()

async def registered_usernames(season_id:int):
    db=await connect()
    try:
        cur=await db.execute("SELECT proavalon_username FROM season_players WHERE community_season_id=? ORDER BY proavalon_username COLLATE NOCASE",(season_id,))
        return [r['proavalon_username'] for r in await cur.fetchall()]
    finally: await db.close()

async def all_links_for_discord(guild_id:int, discord_id:int):
    db=await connect()
    try:
        cur=await db.execute("SELECT a.*, c.required_prefix,c.display_name,c.id AS community_season_id,c.started_at,c.ended_at,c.is_active FROM account_links a LEFT JOIN community_seasons c ON c.guild_id=a.guild_id AND c.season_number=a.season_number WHERE a.guild_id=? AND a.discord_user_id=? AND a.is_active=1 ORDER BY c.id ASC,a.linked_at ASC",(str(guild_id),str(discord_id)))
        return await cur.fetchall()
    finally: await db.close()

async def link_for_username(guild_id:int, username:str):
    db=await connect()
    try:
        cur=await db.execute("SELECT a.*,c.required_prefix,c.display_name,c.id AS community_season_id,c.is_active FROM account_links a LEFT JOIN community_seasons c ON c.guild_id=a.guild_id AND c.season_number=a.season_number WHERE a.guild_id=? AND a.proavalon_username=? COLLATE NOCASE AND a.is_active=1 ORDER BY c.is_active DESC,a.linked_at DESC LIMIT 1",(str(guild_id),username))
        return await cur.fetchone()
    finally: await db.close()

async def ratings_for_usernames(usernames):
    names=[str(x) for x in usernames if str(x).strip()]
    if not names:
        return {}
    db=await connect()
    try:
        placeholders=','.join('?' for _ in names)
        cur=await db.execute(
            f"SELECT proavalon_username,rating,rating_bracket FROM proavalon_ratings WHERE proavalon_username COLLATE NOCASE IN ({placeholders})",
            tuple(names),
        )
        rows=await cur.fetchall()
        return {str(r['proavalon_username']).casefold():r for r in rows}
    finally:
        await db.close()

async def bot_status_data(guild_id:int):
    db=await connect()
    try:
        async def one(sql):
            c=await db.execute(sql,(str(guild_id),)); return await c.fetchone()
        active=await one("SELECT * FROM community_seasons WHERE guild_id=? AND is_active=1 LIMIT 1")
        commands=await one("SELECT channel_id FROM command_channel_configs WHERE guild_id=? LIMIT 1")
        feedback=await one("SELECT channel_id FROM feedback_channel_configs WHERE guild_id=? LIMIT 1")
        daily=await one("SELECT * FROM daily_leaderboard_configs WHERE guild_id=? LIMIT 1")
        matchcfg=await one("SELECT * FROM season_match_configs WHERE guild_id=? LIMIT 1")
        matches=players=0
        if active:
            c=await db.execute("SELECT COUNT(*) AS n FROM season_matches WHERE community_season_id=?",(active['id'],)); matches=int((await c.fetchone())['n'])
            c=await db.execute("SELECT COUNT(*) AS n FROM season_players WHERE community_season_id=?",(active['id'],)); players=int((await c.fetchone())['n'])
        return {'active':active,'commands':commands,'feedback':feedback,'daily':daily,'matchcfg':matchcfg,'matches':matches,'players':players}
    finally: await db.close()

def unpack(row):
    players=_j(row['players_json'],[]); roles=_j(row['roles_json'],[])
    out=[]
    for i,u in enumerate(players):
        role=roles[i] if i<len(roles) else None
        out.append({'username':str(u),'role':role,'alignment':alignment(role)})
    return out

def player_games(rows, username):
    target=username.casefold(); result=[]
    for row in rows:
        p=next((x for x in unpack(row) if x['username'].casefold()==target),None)
        if not p: continue
        won=(p['alignment'] is not None and p['alignment']==winner_norm(row['winner']))
        result.append((row,p,won))
    return result

def player_summary(rows,username):
    games=player_games(rows,username)
    wins=sum(1 for _,_,w in games if w); losses=len(games)-wins
    return {'games':len(games),'wins':wins,'losses':losses,'wr':wins/len(games)*100 if games else 0,'first':games[0][0]['finished_at'] if games else None,'last':games[-1][0]['finished_at'] if games else None}

def streak_summary(rows,username):
    games=player_games(rows,username)
    best_w=best_l=cur_w=cur_l=0
    for _,_,won in games:
        if won: cur_w+=1; cur_l=0; best_w=max(best_w,cur_w)
        else: cur_l+=1; cur_w=0; best_l=max(best_l,cur_l)
    current=0; kind='None'
    if games:
        last=games[-1][2]; kind='Win' if last else 'Loss'; current=1
        for _,_,w in reversed(games[:-1]):
            if w==last: current+=1
            else: break
    return {'games':len(games),'current':current,'current_kind':kind,'longest_win':best_w,'longest_loss':best_l}

def role_summary(rows,username):
    d=defaultdict(lambda:[0,0])
    for _,p,won in player_games(rows,username):
        role=p['role'] or 'Unknown'; d[role][0]+=1; d[role][1]+=int(won)
    return sorted([{'role':r,'games':g,'wins':w,'losses':g-w,'wr':w/g*100 if g else 0} for r,(g,w) in d.items()], key=lambda x:(-x['games'],-x['wr'],x['role']))

def alignment_summary(rows,username):
    d={'Resistance':[0,0],'Spy':[0,0]}
    for _,p,won in player_games(rows,username):
        if p['alignment'] in d: d[p['alignment']][0]+=1; d[p['alignment']][1]+=int(won)
    return {k:{'games':g,'wins':w,'losses':g-w,'wr':w/g*100 if g else 0} for k,(g,w) in d.items()}

def role_board(rows,role):
    target=role.casefold(); d=defaultdict(lambda:[0,0])
    for row in rows:
        win=winner_norm(row['winner'])
        for p in unpack(row):
            if str(p['role'] or '').casefold()!=target: continue
            d[p['username']][0]+=1; d[p['username']][1]+=int(p['alignment']==win)
    return [{'username':u,'games':g,'wins':w,'wr':w/g*100 if g else 0} for u,(g,w) in d.items()]

def activity_summary(rows,username,days=30):
    games=player_games(rows,username)
    cutoff=datetime.now(timezone.utc)-timedelta(days=days)
    recent=[]; by_day=defaultdict(int)
    for row,p,w in games:
        try: dt=datetime.fromisoformat(str(row['finished_at']).replace('Z','+00:00'))
        except Exception: continue
        if dt>=cutoff: recent.append((row,p,w)); by_day[dt.strftime('%Y-%m-%d')]+=1
    busiest=max(by_day.items(), key=lambda kv:kv[1], default=(None,0))
    return {'games':len(recent),'wins':sum(1 for *_,w in recent if w),'days':days,'busiest_day':busiest[0],'busiest_count':busiest[1],'last':games[-1][0]['finished_at'] if games else None}

def relationships(rows,username):
    target=username.casefold(); d=defaultdict(lambda:{'shared':0,'same':0,'opp':0,'opp_wins':0,'opp_losses':0})
    for row,p,won in player_games(rows,username):
        for other in unpack(row):
            if other['username'].casefold()==target: continue
            r=d[other['username']]; r['shared']+=1
            if p['alignment'] and other['alignment']:
                if p['alignment']==other['alignment']: r['same']+=1
                else:
                    r['opp']+=1; r['opp_wins']+=int(won); r['opp_losses']+=int(not won)
    out=[]
    for u,r in d.items():
        r=dict(r,username=u,same_pct=r['same']/r['shared']*100 if r['shared'] else 0,opp_pct=r['opp']/r['shared']*100 if r['shared'] else 0)
        out.append(r)
    return out

def season_records(rows,min_games=5):
    names={p['username'] for row in rows for p in unpack(row)}
    summaries=[]
    for username in names:
        base=dict(username=username,**player_summary(rows,username))
        streaks=streak_summary(rows,username)
        aligns=alignment_summary(rows,username)
        base['longest_win_streak']=streaks['longest_win']
        base['res']=aligns['Resistance']
        base['spy']=aligns['Spy']
        summaries.append(base)

    eligible=[x for x in summaries if x['games']>=min_games]
    res_eligible=[x for x in summaries if x['res']['games']>=min_games]
    spy_eligible=[x for x in summaries if x['spy']['games']>=min_games]

    return {
        'most_games':max(summaries,key=lambda x:x['games'],default=None),
        'best_wr':max(eligible,key=lambda x:(x['wr'],x['games']),default=None),
        'worst_wr':min(eligible,key=lambda x:(x['wr'],-x['games']),default=None),
        'highest_win_streak':max(summaries,key=lambda x:(x['longest_win_streak'],x['games']),default=None),
        'lowest_win_streak':min(eligible,key=lambda x:(x['longest_win_streak'],-x['games']),default=None),
        'best_res':max(res_eligible,key=lambda x:(x['res']['wr'],x['res']['games']),default=None),
        'worst_res':min(res_eligible,key=lambda x:(x['res']['wr'],-x['res']['games']),default=None),
        'best_spy':max(spy_eligible,key=lambda x:(x['spy']['wr'],x['spy']['games']),default=None),
        'worst_spy':min(spy_eligible,key=lambda x:(x['spy']['wr'],-x['spy']['games']),default=None),
        'usernames':sorted(names,key=str.casefold),
        'unique_players':len(names),
        'matches':len(rows),
    }
