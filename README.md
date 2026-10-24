# ProAvalon Discord Bot

A Discord bot and cloud recording system for ProAvalon communities.

Pbot automatically tracks qualifying ProAvalon matches and provides season
management, account linking, statistics, ratings, leaderboards, match history,
social statistics, community tools, and searchable match recordings directly
through Discord.

## Features

- Automatic ProAvalon match detection and archival
- Multi-server support with independent season configuration
- Season creation, reset, archival, and username-prefix enforcement
- Season-specific Discord and ProAvalon account linking
- Player profiles, ratings, win rates, streaks, records, and activity history
- Overall, Resistance, Spy, role, teammate, rival, and social statistics
- Cached and scheduled leaderboards
- Match history with teams, roles, missions, voting history, and recordings
- Paginated match search by one or more players
- Historical player statistics across seasons
- Voice-channel lobby pings with optional custom messages
- Identification of unregistered ProAvalon players
- Configurable command, feedback, match-history, and recording channels
- Server-level bot administrator management
- Persistent recording audits for diagnosing individual rooms

Use `/commands` in Discord to browse the commands available to your account.

## Account Linking

Players link their Discord account to their active-season ProAvalon account
with:

```text
/account link
```

Links are stored per season. Starting a new season does not overwrite the
account associations or statistics preserved for older seasons.

## Seasons

Each Discord server maintains its own ProAvalon season configuration. A season
can require a username prefix so only matching accounts are registered and
tracked. The active prefix is also supplied dynamically to the recording
workers; recorder services do not need a hard-coded season prefix during normal
operation.

## Statistics and Match History

Pbot stores completed match data and exposes commands for:

- Player and role performance
- Recent win/loss history
- Leaderboards and daily leaderboard posts
- Best overall, Resistance, and Spy teammates
- Most-dominated opponents
- Match lookup by participating players
- Season records and social comparisons

When a YouTube recording is available, Pbot updates the existing match-history
message with the video link while retaining the original voting chart.

## Automatic Match Recording

Recording is split across three independently deployed services:

1. **Pbot (`proavalon-discord-bot`)** tracks rooms and seasons, coordinates
   recorder claims, stores YouTube authorization, uploads match metadata, and
   updates Discord match history.
2. **`recorder-worker`** runs an authenticated Playwright browser, records the
   ProAvalon table, controls Craig, combines media with FFmpeg, and uploads the
   final MP4 through Pbot's YouTube integration.
3. **Craig (`craig`)** joins Discord voice chat with a dedicated recorder bot
   and produces the mixed voice audio. Craig uses its own Postgres, Redis, and
   persistent volume.

The services communicate over authenticated endpoints and Railway private
networking. The user's computer does not need to remain on after the services
are deployed.

### Recording lifecycle

For each eligible public ten-player game, the worker:

1. Claims the room through Pbot so two workers cannot record the same match.
2. Opens the room with a dedicated ProAvalon spectator account.
3. Applies the recording layout: dark mode, split view, Game Chat, and Vote
   History.
4. Starts browser capture when the game begins and asks Craig to record the
   matching Discord voice channel.
5. Includes a Discord speaker overlay in the captured browser video.
6. Records up to 30 seconds of postgame, ending sooner when a replacement lobby
   is ready.
7. Converts the browser capture, downloads Craig's mixed audio, synchronizes the
   streams, and creates the final MP4 with FFmpeg.
8. Uploads the video to YouTube and associates it with the archived match.
9. Updates the existing Discord match-history message with the YouTube link.
10. Deletes completed browser, audio, final-video, and Craig source media after
    upload confirmation.

Conversion and upload run in the background. The browser can begin capturing a
replacement game while the previous match is being processed.

### Cancellation and recovery

The recording system handles common lifecycle failures without treating them as
valid completed matches:

- Pregame rooms that fall below ten players are discarded.
- Administrators can run `/recording halt` to discard an abandoned recording.
- ProAvalon games that reach the `Voided` phase are automatically discarded;
  the browser and Craig leave so the next lobby can be recorded.
- An abandoned room can be replaced when a new lobby contains enough returning
  players.
- Worker restarts can recover a tracked Craig session or discard an untracked
  orphan safely.
- Missing or expired Craig sources stop retrying and release their local media.
- YouTube authorization failures retain completed media for the configured
  retention period instead of immediately destroying a recoverable upload.
- A configurable free-space guard prevents new recordings from exhausting the
  worker volume.

Use these administrator commands to inspect or control recording:

```text
/recording status
/recording audit room_id:<ProAvalon room ID>
/recording halt
/recording youtube enable
/recording youtube disable
```

## YouTube Integration

YouTube is connected per Discord server with `/recording youtube enable`. Pbot
stores the refresh token encrypted and the recorder never receives the Google
client secret or refresh token. Videos are uploaded using resumable YouTube
upload sessions and are `unlisted` by default unless configured otherwise.

Google OAuth applications left in **Testing** mode normally expire refresh
authorization after seven days. Pbot warns the authorizing administrator one
day before the expected expiration. Run `/recording youtube enable` again and
complete Google sign-in to renew it.

YouTube upload limits are enforced by both the Google Cloud project and the
connected YouTube channel. When a limit is reached, uploads cannot resume until
the applicable quota or channel allowance resets.

Pbot requires these YouTube-related environment variables:

```text
PUBLIC_BASE_URL
GOOGLE_YOUTUBE_CLIENT_ID
GOOGLE_YOUTUBE_CLIENT_SECRET
YOUTUBE_OAUTH_STATE_SECRET
YOUTUBE_TOKEN_ENCRYPTION_KEY
YOUTUBE_PRIVACY_STATUS
YOUTUBE_OAUTH_TESTING_MODE
```

Never commit their real values.

## Recorder Bridge

Set `RECORDER_BRIDGE_SECRET` to the same long random value on Pbot and every
authorized recorder worker. Set each worker's `RECORDER_BRIDGE_URL` to Pbot's
public Railway domain.

The bridge provides active-season configuration, lobby snapshots, account-link
information needed for voice matching, atomic room claims, cancellation
requests, recording audits, and YouTube handoff endpoints. It does not expose
Discord tokens, ProAvalon passwords, ratings, or general match-history data.

## Railway Deployment

The production project uses separate Railway services:

- `proavalon-discord-bot`
- `recorder-worker`
- `craig`
- Postgres for Craig
- Redis for Craig

Pbot, the recorder worker, and Craig each use their own persistent storage as
appropriate. `recorder-worker` and Craig must not share a volume.

### Recorder-worker configuration

Create the worker from this repository with `/services/recorder-worker` as its
Railway root directory and mount a persistent volume at `/data`.

Required configuration includes:

```text
RECORDER_PROAVALON_USERNAME
RECORDER_PROAVALON_PASSWORD
RECORDER_INSTANCE_ID
RECORDER_CLAIM_DELAY_SECONDS
RECORDER_BRIDGE_URL
RECORDER_BRIDGE_SECRET
DISCORD_GUILD_ID
CRAIG_CONTROL_URL
CRAIG_CONTROL_TOKEN
CRAIG_DOWNLOAD_URL
VIDEO_OUTPUT_DIR=/data/browser
AUDIO_OUTPUT_DIR=/data/audio
FINAL_OUTPUT_DIR=/data/final
```

Useful safety and synchronization settings include:

```text
RECORDER_ORPHAN_RETENTION_HOURS=24
RECORDER_INCOMPLETE_RETENTION_MINUTES=30
RECORDER_UPLOAD_MAX_ATTEMPTS=5
RECORDER_MIN_FREE_GB=1
RECORDER_MAX_GAME_SECONDS=5400
RECORDER_AUDIO_SYNC_CORRECTION_SECONDS=2.0
```

See `services/recorder-worker/.env.example` for the maintained checklist.

### Craig configuration and cleanup

Create Craig from `/services/craig`, mount its persistent volume at `/data`, and
connect it to the managed Postgres and Redis services. Craig uses a dedicated
Discord bot application and a control token shared only with its recorder
worker.

The important storage paths are:

```text
REC_DIRECTORY=/data/rec
DOWNLOADS_DIRECTORY=/data/downloads
OUTPUT_DIRECTORY=/data/output
TMP_DIRECTORY=/tmp/craig
```

Recommended cleanup settings for an active recording server are:

```text
TASKS_IGNORE=refreshPatrons
KITCHEN_CLEAN_FILES=true
JOB_EXPIRATION=20m
KITCHEN_CRON_TIME=*/5 * * * *
RECORDING_CLEAN_CRON=*/10 * * * *
RECORDING_FALLBACK_EXPIRATION=2h
RECORDING_SKIP_ALL=false
```

`TASKS_IGNORE` must not include `cleanRecordings`. The short Kitchen retention
removes generated mixes after the worker has downloaded them, while successful
YouTube handoffs explicitly delete Craig's raw source recording.

### Multiple simultaneous recorders

Each simultaneous recorder requires its own recorder-worker, Craig service,
dedicated Discord recorder bot, dedicated ProAvalon spectator account, and
volume. Assign stable worker identities such as `recorder-1` and `recorder-2`.

Use a claim delay of `0` for the primary worker and a small delay such as `3`
seconds for the secondary. Pbot atomically leases rooms, allowing a second
worker to claim a simultaneous lobby without altering the first recorder.

## Data Storage

Pbot uses SQLite on persistent storage for server configuration, seasons,
account links, ratings, match history, recording audits, and YouTube
integration metadata. Craig uses Postgres and Redis for its own recording
services. Railway volumes preserve application data across deployments.

Large recording artifacts are temporary and should not be treated as backups.
They are deleted after successful upload or by bounded cleanup policies.

## Privacy and Security

Tokens, passwords, OAuth credentials, encryption keys, control tokens, and
bridge secrets must be supplied through environment variables and must never be
committed to the repository.

If a secret appears in a terminal log, screenshot, commit, or public message,
rotate it immediately in the service that issued it. Making this repository
public is safe only after reviewing the complete Git history for previously
committed credentials.
