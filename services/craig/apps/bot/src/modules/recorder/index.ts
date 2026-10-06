import { prisma } from '@craig/db';
import type Dysnomia from '@projectdysnomia/dysnomia';
import { CronJob } from 'cron';
import { access, mkdir, readdir, unlink } from 'fs/promises';
import path from 'path';
import semver from 'semver';
import { ComponentType, MessageFlags } from 'slash-create';

import type { CraigBot } from '../../bot.js';
import { checkMaintenance } from '../../redis.js';
import { cutoffText, getSelfMember, isChannelNotFull, parseRewards } from '../../util.js';
import { BotModule } from '../../runtime.js';
import Recording, { RecordingState } from './recording.js';
import { VoiceTestState } from './voiceTest.js';

const RECORDING_TTL = 5 * 60 * 1000;
const VOICE_OBSERVATORY_URL = 'https://vo-api.snaz.in';
const VOICE_OBSERVATORY_TIMEOUT = 1500;

interface VoiceTraceOptions {
  connection: Dysnomia.VoiceConnection | null;
  voiceVersion?: string;
  rtcWorkerVersion?: string;
  code?: number;
}

export default class RecorderModule extends BotModule {
  recordings = new Map<string, Recording>();
  voiceTests = new Map<string, import('./voiceTest.js').default>();
  recordingPath: string;
  recordingsChecked = false;
  cron: CronJob;
  private readonly handleReady = this.onReady.bind(this);
  private readonly handleVoiceStateUpdate = this.onVoiceStateUpdate.bind(this);
  private readonly handleGuildLeave = this.onGuildLeave.bind(this);
  private readonly handleGuildUnavailable = this.onGuildUnavailable.bind(this);

  constructor(client: CraigBot) {
    super(client, {
      name: 'recorder',
      description: 'Recording handler'
    });

    this.recordingPath = path.resolve(this.client.config.craig.recordingFolder);
    this.cron = new CronJob('* * * * *', this.onCron.bind(this), null, false, 'America/New_York');
  }

  get uploader() {
    return this.client.upload;
  }

  get metrics() {
    return this.client.metrics;
  }

  async load() {
    this.client.bot.on('ready', this.handleReady);
    this.client.bot.on('voiceStateUpdate', this.handleVoiceStateUpdate);
    this.client.bot.on('guildDelete', this.handleGuildLeave);
    this.client.bot.on('guildUnavailable', this.handleGuildUnavailable);
    this.cron.start();

    try {
      await access(this.recordingPath);
    } catch (e) {
      this.logger.info('Recording folder not found, creating...');
      await mkdir(this.recordingPath);
      return;
    }
  }

  async onReady() {
    this.checkForErroredRecordings();
  }

  unload() {
    this.client.bot.removeListener('ready', this.handleReady);
    this.client.bot.removeListener('voiceStateUpdate', this.handleVoiceStateUpdate);
    this.client.bot.removeListener('guildDelete', this.handleGuildLeave);
    this.client.bot.removeListener('guildUnavailable', this.handleGuildUnavailable);
    this.cron.stop();
  }

  onCron() {
    const now = Date.now();
    for (const [guildID, recording] of this.recordings.entries()) {
      if (
        (recording.state === RecordingState.IDLE || recording.state === RecordingState.CONNECTING) &&
        now - recording.createdAt.valueOf() > RECORDING_TTL
      ) {
        this.logger.warn(
          `Recording ${recording.id} seems to be a dead recording, removing from map... (${guildID}:${recording.state}, by ${
            recording.user.id
          }, created ${recording.createdAt.toISOString()})`
        );
        this.recordings.delete(guildID);
      }
    }

    for (const [guildID, voiceTest] of this.voiceTests.entries()) {
      if (now - voiceTest.createdAt.valueOf() > RECORDING_TTL) {
        this.logger.warn(
          `Voice test seems to be a stale test, removing from map... (${guildID}:${voiceTest.state}, by ${
            voiceTest.user.id
          }, created ${voiceTest.createdAt.toISOString()})`
        );
        this.voiceTests.delete(guildID);
      }
    }
  }

  find(id: string) {
    for (const recording of this.recordings.values()) {
      if (recording.id === id) return recording;
    }
  }

  async startControlled(guildID: string, channelID: string, requesterID: string, postChannelID?: string) {
    if (this.recordings.has(guildID)) throw new Error('This guild already has an active recording.');
    const guild = this.client.bot.guilds.get(guildID);
    if (!guild) throw new Error('Guild not found on this shard.');
    const channel = guild.channels.get(channelID);
    if (!channel || (channel.type !== 2 && channel.type !== 13)) throw new Error('Voice channel not found.');
    if (!channel.permissionsOf(this.client.bot.user.id).has('voiceConnect')) throw new Error('Missing Connect permission.');
    if (!isChannelNotFull(channel as any, this.client.bot.user.id)) throw new Error('Voice channel is full.');
    if (!guild.permissionsOf(this.client.bot.user.id).has('changeNickname')) throw new Error('Missing Change Nickname permission.');

    const member = guild.members.get(requesterID) || (await guild.fetchMembers({ userIDs: [requesterID] }))[0];
    if (!member) throw new Error('Configured requester is not a member of this guild.');

    const selfUser = await getSelfMember(guild, this.client.bot);
    const recNick = cutoffText(`![RECORDING] ${selfUser ? (selfUser.nick ?? selfUser.username) : this.client.bot.user.username}`, 32);
    if (selfUser && (!selfUser.nick || !selfUser.nick.includes('[RECORDING]')))
      await this.client.bot.editGuildMember(guildID, '@me', { nick: recNick }, 'Starting automatic ProAvalon recording');

    const recording = new Recording(this, channel as any, member.user, true);
    this.recordings.set(guildID, recording);
    if (postChannelID) {
      const postChannel = guild.channels.get(postChannelID);
      if (postChannel?.permissionsOf(this.client.bot.user.id).has('sendMessages') && postChannel.permissionsOf(this.client.bot.user.id).has('embedLinks')) {
        const message = await this.client.bot.createMessage(postChannelID, recording.messageContent() as any).catch(() => null);
        if (message) {
          recording.messageID = message.id;
          recording.messageChannelID = message.channel.id;
        }
      }
    }

    // Controlled recordings are an internal feature of this dedicated,
    // self-hosted recorder. Give them the owner tier so Ferret can create the
    // mixed MP3 required by the ProAvalon media pipeline.
    await recording.start(parseRewards(this.client.config, -1, -1), false);
    if (recording.state === RecordingState.ERROR) throw new Error(recording.stateDescription || 'Craig failed to start recording.');
    return {
      id: recording.id,
      accessKey: recording.accessKey,
      deleteKey: recording.deleteKey,
      state: RecordingState[recording.state],
      startedAt: recording.startedAt?.toISOString() ?? null
    };
  }

  controlledStatus(guildID: string) {
    const recording = this.recordings.get(guildID);
    if (!recording) return { active: false };
    return { active: true, id: recording.id, channelId: recording.channel.id, state: RecordingState[recording.state] };
  }

  controlledSpeakers(guildID: string) {
    const recording = this.recordings.get(guildID);
    if (!recording) return { active: false, speakers: [] };
    return {
      active: true,
      channelId: recording.channel.id,
      speakers: recording.speakerSnapshot()
    };
  }

  async stopControlled(guildID: string, discard = false) {
    const recording = this.recordings.get(guildID);
    if (!recording) return { stopped: false };
    const result = { stopped: true, discarded: discard, id: recording.id, accessKey: recording.accessKey, deleteKey: recording.deleteKey };
    await recording.stop(discard);
    if (discard) {
      const prefix = `${recording.id}.ogg`;
      const files = await readdir(this.recordingPath).catch(() => []);
      await Promise.all(files.filter((file) => file.startsWith(prefix)).map((file) => unlink(path.join(this.recordingPath, file)).catch(() => {})));
      await prisma.recording.delete({ where: { id: recording.id } }).catch(() => {});
    }
    return result;
  }

  traceVoiceConnection({ connection, voiceVersion, rtcWorkerVersion }: VoiceTraceOptions) {
    const endpoint = connection?.endpoint?.host;
    if (!endpoint || !voiceVersion || !rtcWorkerVersion) return;
    this.postVoiceObservatory('/v1/traces/voice', {
      endpoint,
      dave_version: connection?.daveProtocolVersion ?? 0,
      voice_version: voiceVersion,
      rtc_worker_version: rtcWorkerVersion
    });
  }

  traceVoiceError({ connection, voiceVersion, rtcWorkerVersion, code }: VoiceTraceOptions) {
    if (code === undefined || (code > 0 && code <= 1000)) return;
    this.postVoiceObservatory('/v1/traces/error', {
      endpoint: connection?.endpoint?.host,
      code,
      dave_version: connection?.daveProtocolVersion ?? 0,
      voice_version: voiceVersion,
      rtc_worker_version: rtcWorkerVersion
    });
  }

  traceVoiceTimeout(connection: Dysnomia.VoiceConnection | null) {
    this.postVoiceObservatory('/v1/traces/error', {
      endpoint: connection?.endpoint?.host,
      code: 0
    });
  }

  parseVoiceDisconnectCode(err: Error) {
    const match = err.message.match(/\b(\d{4})\b/);
    return match ? Number(match[1]) : undefined;
  }

  private postVoiceObservatory(path: string, body: Record<string, string | number | undefined>) {
    const token = process.env.VOICE_OBSERVATORY_TOKEN;
    if (!token) return;

    void globalThis
      .fetch(`${VOICE_OBSERVATORY_URL}${path}`, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${token}`,
          'Content-Type': 'application/json'
        },
        body: JSON.stringify(Object.fromEntries(Object.entries(body).filter(([, value]) => value !== undefined))),
        signal: AbortSignal.timeout(VOICE_OBSERVATORY_TIMEOUT)
      })
      .catch(() => {});
  }

  async pushVoiceVersions(voiceEndpoint: string, voiceVersion: string, rtcWorkerVersion: string) {
    const regionId = voiceEndpoint.startsWith('c-')
      ? voiceEndpoint.replace(/\d+?-[\da-f]+\.discord\.media$/, '')
      : voiceEndpoint.replace(/\d+\.discord\.media$/, '');

    this.metrics.onVoiceServerConnect(regionId);

    const existingRegion = await prisma.voiceRegion.findUnique({ where: { id: regionId } });

    await prisma.voiceRegion.upsert({
      where: { id: regionId },
      update: {},
      create: { id: regionId }
    });

    // Detect and track versions
    const voiceVersionSeen = await prisma.voiceVersion.findUnique({
      where: { version: voiceVersion }
    });

    if (!voiceVersionSeen)
      await prisma.voiceVersion.create({
        data: { version: voiceVersion, regionId, endpoint: voiceEndpoint }
      });

    const rtcWorkerVersionSeen = await prisma.rtcVersion.findUnique({
      where: { version: rtcWorkerVersion }
    });

    if (!rtcWorkerVersionSeen)
      await prisma.rtcVersion.create({
        data: { version: rtcWorkerVersion, regionId, endpoint: voiceEndpoint }
      });

    // Update latest region voice version
    const lastVoiceVersion = await prisma.regionVoiceVersion.findUnique({
      where: { regionId }
    });

    if (!lastVoiceVersion || semver.lt(lastVoiceVersion.version, voiceVersion))
      await prisma.regionVoiceVersion.upsert({
        where: { regionId },
        update: {
          version: voiceVersion,
          endpoint: voiceEndpoint,
          seenAt: new Date()
        },
        create: {
          regionId,
          version: voiceVersion,
          endpoint: voiceEndpoint
        }
      });

    // Update latest rtc worker version
    const lastRtcWorkerVersion = await prisma.regionRtcVersion.findUnique({
      where: { regionId }
    });

    if (!lastRtcWorkerVersion || semver.lt(lastRtcWorkerVersion.version, rtcWorkerVersion))
      await prisma.regionRtcVersion.upsert({
        where: { regionId },
        update: {
          version: rtcWorkerVersion,
          endpoint: voiceEndpoint,
          seenAt: new Date()
        },
        create: {
          regionId,
          version: rtcWorkerVersion,
          endpoint: voiceEndpoint
        }
      });

    if (!existingRegion || !voiceVersionSeen || !rtcWorkerVersionSeen) {
      const title = `New ${[
        !existingRegion ? 'Voice Region' : undefined,
        !voiceVersionSeen ? 'Voice Version' : undefined,
        !rtcWorkerVersionSeen ? 'RTC Worker Version' : undefined
      ]
        .filter((v) => !!v)
        .join(', ')}`;
      await fetch(`${this.client.config.craig.systemNotificationURL}?with_components=true`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({
          flags: MessageFlags.IS_COMPONENTS_V2,
          components: [
            {
              type: ComponentType.CONTAINER,
              accent_color: 5793266,
              components: [
                {
                  type: ComponentType.TEXT_DISPLAY,
                  content: [
                    `# 🔊 ${title}`,
                    `**Region:** \`${regionId}\` ${!existingRegion ? '**🆕**' : ''}`,
                    `**Voice Version:** ${voiceVersion} ${!voiceVersionSeen ? '**🆕**' : ''}`,
                    `**RTC Worker Version:** ${rtcWorkerVersion} ${!rtcWorkerVersionSeen ? '**🆕**' : ''}`
                  ].join('\n')
                },
                {
                  type: ComponentType.SEPARATOR
                },
                {
                  type: ComponentType.TEXT_DISPLAY,
                  content: [
                    `**Voice Server Endpoint:** \`${voiceEndpoint}\``,
                    `**Bot**: <@${this.client.bot.user.id}> (${this.client.bot.user.id})`,
                    `-# <t:${Math.floor(Date.now() / 1000)}:F>`
                  ].join('\n')
                }
              ]
            }
          ]
        })
      });
    }
  }

  async checkForErroredRecordings(force = false) {
    if (this.recordingsChecked && !force) return;
    this.recordingsChecked = true;
    const badRecordings = (
      await prisma.recording.findMany({
        where: {
          clientId: this.client.bot.user.id,
          shardId: this.client.shard.id,
          errored: false,
          endedAt: null
        }
      })
    ).filter((br) => !this.find(br.id));

    this.logger.info(
      `Found ${badRecordings.length} errored recordings.`,
      badRecordings.map((br) => br.id)
    );
    if (!badRecordings.length) return;

    // Make craig leave from dead channels
    const guildIds = [...new Set<string>(badRecordings.map((r) => r.guildId))];
    for (const guildId of guildIds) {
      const recording = this.recordings.get(guildId);
      if (recording) continue;

      const guild = this.client.bot.guilds.get(guildId);
      if (!guild) continue;

      const channel = guild.channels.get(badRecordings.find((r) => r.guildId === guildId)!.channelId) as
        | Dysnomia.StageChannel
        | Dysnomia.VoiceChannel;
      if (!channel) continue;

      await channel.join().catch(() => null);
      channel.leave();
    }

    // Warn users
    const userIds = [...new Set<string>(badRecordings.map((r) => r.userId))];
    for (const userId of userIds) {
      const user = this.client.bot.users.get(userId);
      if (!user) continue;

      const dmChannel = await user.getDMChannel().catch(() => null);
      if (!dmChannel) continue;

      await dmChannel
        .createMessage(
          `**⚠️ The following recordings have abruptly ended, please start a new recording from the slash command as the recording interface is no longer valid.**\n\n${badRecordings
            .filter((r) => r.userId === userId)
            .map((r) => `- \`${r.id}\` in <#${r.channelId}>`)
            .join('\n')}`
        )
        .catch(() => null);
    }

    // Delete errored recordings
    for (const recording of badRecordings) {
      await prisma.recording.delete({ where: { id: recording.id } }).catch(() => {});
    }
  }

  async checkForMaintenance() {
    const maintenance = await checkMaintenance(this.client.bot.user.id);
    if (maintenance) {
      const recordings = Array.from(this.recordings.values());
      for (const recording of recordings) {
        if (recording.state === RecordingState.RECORDING) {
          recording.maintenceWarned = true;
          recording.pushToActivity('⚠️ The bot is undergoing maintenance, recording will be stopped.', false);
          recording.stateDescription = `__The bot is undergoing maintenance.__${maintenance.message ? `\n\n${maintenance.message}` : ''}`;
          await recording.stop().catch(() => null);
        }
      }
    }
  }

  onVoiceStateUpdate(member: Dysnomia.Member, oldState: Dysnomia.OldVoiceState) {
    const recording = this.recordings.get(member.guild.id);
    if (!recording) return;

    recording.onVoiceStateUpdate(member, oldState);
  }

  async onGuildLeave(guild: Dysnomia.PossiblyUncachedGuild) {
    if (this.recordings.has(guild.id)) {
      const recording = this.recordings.get(guild.id)!;
      this.logger.warn(`Left guild ${guild.id} during a recording... (${recording.id})`);
      recording.state = RecordingState.ERROR;
      recording.stateDescription = '⚠️ This guild went unavailable during a voice test! To prevent further errors, this voice test has ended.';
      await recording.stop(true).catch(() => {});
      await recording.updateMessage();
    }
    if (this.voiceTests.has(guild.id)) {
      const voiceTest = this.voiceTests.get(guild.id)!;
      this.logger.warn(`Left guild ${guild.id} during a voice test...`);
      voiceTest.state = VoiceTestState.ERROR;
      voiceTest.stateDescription = '⚠️ This guild went unavailable during a voice test! To prevent further errors, this voice test has ended.';
      await voiceTest.cancel().catch(() => {});
      await voiceTest.updateMessage();
    }
  }

  async onGuildUnavailable(guild: Dysnomia.UnavailableGuild) {
    this.logger.warn(`Guild ${guild.id} is now unavailable...`);
    if (this.recordings.has(guild.id)) {
      const recording = this.recordings.get(guild.id)!;
      this.logger.warn(`Guild ${guild.id} went unavailable during a recording... (${recording.id})`);
      recording.state = RecordingState.ERROR;
      recording.stateDescription = '⚠️ This guild went unavailable during recording! To prevent further errors, this recording has ended.';
      await recording.stop(true).catch(() => {});
      await recording.updateMessage();
    }
    if (this.voiceTests.has(guild.id)) {
      const voiceTest = this.voiceTests.get(guild.id)!;
      this.logger.warn(`Left guild ${guild.id} during a voice test...`);
      voiceTest.state = VoiceTestState.ERROR;
      voiceTest.stateDescription = '⚠️ This guild went unavailable during a voice test! To prevent further errors, this voice test has ended.';
      await voiceTest.cancel().catch(() => {});
      await voiceTest.updateMessage();
    }
  }
}
