# Recorder worker

The recording code is stored in the same repository as Pbot, while running in
an isolated worker process so Chromium or FFmpeg cannot take the Discord bot
offline.

`recorder-worker` launches two cooperating processes in one container:

- the ProAvalon browser recorder writes room MP4s to `/data/browser`;
- the audio/media controller operates Craig, downloads the mixed voice audio,
  combines files with the same ProAvalon room ID, uploads the final MP4, and
  reports the YouTube video ID to Pbot.

The worker requires a persistent Railway volume mounted at `/data`. Craig
remains a separate service and is reached through Railway private networking.
Do not add the worker to Pbot's own start command; create another Railway
service from this repository with `services/recorder-worker` as its root
directory.

The customized Craig build is in `services/craig`. On Railway, create managed
Postgres and Redis services, then create a GitHub service rooted at
`/services/craig`. Attach a persistent volume at `/data` and use
`services/craig/railway.env.example` as the variable checklist. Craig does not
need a public domain: `recorder-worker` reaches ports 9300 and 9100 over
Railway private networking.

Keep the proven local setup available until the hosted worker and Craig have
completed an end-to-end match test.

## Multiple simultaneous recorders

Each recorder pair consists of one Craig service and one recorder-worker
service. Give every worker a stable, unique `RECORDER_INSTANCE_ID` such as
`recorder-1` or `recorder-2`. Pbot atomically leases each ProAvalon room to one
instance, and the worker's browser and audio controller only operate on the
room leased to that identity.

Use `RECORDER_CLAIM_DELAY_SECONDS=0` for the primary worker and `3` for the
secondary worker. This gives Recorder 1 the first opportunity to take a lone
lobby while still allowing Recorder 2 to take a simultaneous second lobby.

Run each Craig service with its own Discord bot token, control token, Postgres,
Redis namespace, and `/data` volume. Run each recorder-worker with its own
ProAvalon account, Craig private-domain URLs, and `/data` volume. Workers may
share Pbot's bridge URL, bridge secret, Discord guild ID, active season, and
server-level YouTube connection.

Deploy Pbot before deploying workers that use room claims. A claim expires
automatically when its worker stops heartbeating, so a crashed worker cannot
reserve a room permanently.
