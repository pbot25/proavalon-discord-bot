\# ProAvalon Discord Bot



A Discord bot built to integrate ProAvalon gameplay with Discord communities.



The bot automatically tracks ProAvalon matches and provides season management, player statistics, ratings, leaderboards, match history, social statistics, groups, and other community features directly through Discord.



\## Features



\- Automatic ProAvalon match tracking

\- Multi-server support

\- Season creation, archiving, and management

\- Season-specific Discord account linking

\- Player profiles and statistics

\- Rating and win-rate leaderboards

\- Resistance and Spy leaderboards

\- Daily automated leaderboards

\- Match history with mission and voting information

\- Role and alignment statistics

\- Win/loss streak tracking

\- Player records and activity statistics

\- Social statistics

\- Rivals and teammate statistics

\- Player history across seasons

\- Custom player groups and group comparisons

\- Online player lobby pings

\- Configurable command and feedback channels

\- Server and bot administrator management



\## Account Linking



Players can link their Discord account to their current ProAvalon season account using:



`/account link`



Account links are stored by season, allowing historical links to remain associated with previous seasons.



\## Seasons



Each Discord server can maintain its own ProAvalon season configuration.



A season can define a required username prefix, allowing only matching ProAvalon accounts to participate in that season's tracking.



Historical seasons and their statistics are preserved when a new season begins.



\## Commands



The bot provides commands for:



\- Accounts

\- Player statistics

\- Leaderboards

\- Match history

\- Seasons

\- Social statistics

\- Groups

\- Community tools

\- Administration



Use `/commands` in Discord to browse the available commands and their descriptions.



\## Data



The bot uses SQLite for persistent data storage.



Production deployments use persistent storage so match history, account links, season information, server configuration, and other bot data survive application redeployments.



\## Hosting



The bot is designed to run continuously as a hosted Discord service and is currently deployed using Railway.



\## Privacy



Sensitive configuration such as Discord tokens and ProAvalon credentials is provided through environment variables and is not stored in the repository.

### Recorder bridge

Set `RECORDER_BRIDGE_SECRET` to a long random value in Railway to enable the
authenticated `/recorder/lobbies` endpoint. Give the local recorder the same
secret and the Railway public domain through `RECORDER_BRIDGE_URL`. Pbot then
provides pregame roster snapshots without sharing its ProAvalon credentials or
opening a competing ProAvalon session. The endpoint never returns passwords,
Discord tokens, ratings, or match-history data.

