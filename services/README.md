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
