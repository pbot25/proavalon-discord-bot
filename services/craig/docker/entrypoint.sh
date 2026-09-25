#!/bin/sh
set -eu

mkdir -p "${REC_DIRECTORY:-/data/rec}" "${DOWNLOADS_DIRECTORY:-/data/downloads}" "${OUTPUT_DIRECTORY:-/data/output}" "${TMP_DIRECTORY:-/tmp/craig}"

wait_for_tcp() {
  name="$1"
  host="$2"
  port="$3"
  timeout="${4:-60}"

  echo "Waiting for $name at $host:$port..."
  node - "$host" "$port" "$timeout" <<'NODE'
const net = require('node:net');

const [, , host, portRaw, timeoutRaw] = process.argv;
const port = Number(portRaw);
const deadline = Date.now() + Number(timeoutRaw) * 1000;

function tryConnect() {
  const socket = net.connect({ host, port });
  socket.setTimeout(1000);
  socket.once('connect', () => {
    socket.destroy();
    process.exit(0);
  });
  socket.once('timeout', () => socket.destroy());
  socket.once('error', () => {});
  socket.once('close', () => {
    if (Date.now() >= deadline) process.exit(1);
    setTimeout(tryConnect, 1000);
  });
}

tryConnect();
NODE
}

database_host_port() {
  node - <<'NODE'
const url = process.env.DATABASE_URL;
if (!url) process.exit(1);
const parsed = new URL(url);
console.log(`${parsed.hostname} ${parsed.port || 5432}`);
NODE
}

if DB_TARGET="$(database_host_port)"; then
  set -- $DB_TARGET
  wait_for_tcp postgres "$1" "$2" "${CRAIG_STARTUP_TIMEOUT:-90}"
fi

wait_for_tcp redis "${REDIS_HOST:-redis}" "${REDIS_PORT:-6379}" "${CRAIG_STARTUP_TIMEOUT:-90}"

PRISMA_CLI="${PRISMA_CLI:-/opt/craig/migrate/node_modules/.bin/prisma}"
PRISMA_CONFIG="${PRISMA_CONFIG:-/opt/craig/migrate/prisma.config.ts}"

if [ "${CRAIG_SKIP_MIGRATIONS:-false}" != "true" ]; then
  if [ ! -x "$PRISMA_CLI" ]; then
    echo "Prisma CLI is missing from the Craig runtime image." >&2
    exit 1
  fi
  if [ ! -f "$PRISMA_CONFIG" ]; then
    echo "Prisma config is missing from the Craig runtime image." >&2
    exit 1
  fi
  "$PRISMA_CLI" migrate deploy --config "$PRISMA_CONFIG"
fi

if [ "${DISCORD_COMMANDS_DISABLED:-false}" = "true" ]; then
  echo "Removing Discord application commands for the dedicated recorder bot..."
  node --input-type=module <<'NODE'
const token = (process.env.BOT_TOKEN || '').replace(/^Bot\s+/i, '');
const applicationId = process.env.BOT_APPLICATION_ID || '';
const guildId = process.env.DEVELOPMENT_GUILD_ID || '';

if (!token || !applicationId) {
  console.warn('Skipping command removal because BOT_TOKEN or BOT_APPLICATION_ID is missing.');
  process.exit(0);
}

async function clearCommands(path, label) {
  const response = await fetch(`https://discord.com/api/v10${path}`, {
    method: 'PUT',
    headers: {
      Authorization: `Bot ${token}`,
      'Content-Type': 'application/json'
    },
    body: '[]'
  });
  if (!response.ok) {
    console.warn(`Could not remove ${label} commands: HTTP ${response.status} ${await response.text()}`);
    return false;
  }
  console.log(`Removed ${label} commands.`);
  return true;
}

await clearCommands(`/applications/${applicationId}/commands`, 'global');
if (guildId) {
  await clearCommands(
    `/applications/${applicationId}/guilds/${guildId}/commands`,
    'server-specific'
  );
}
NODE
fi

exec pm2-runtime /opt/craig/ecosystem.config.cjs
