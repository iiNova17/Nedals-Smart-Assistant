import { createServer } from 'node:http';
import { timingSafeEqual } from 'node:crypto';
import { mkdirSync, readFileSync, writeFileSync, renameSync, existsSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { loadEnvFile } from 'node:process';
import makeWASocket, { DisconnectReason, downloadMediaMessage, normalizeMessageContent } from '@whiskeysockets/baileys';
import pino from 'pino';
import QRCode from 'qrcode';
import { Store } from './store.mjs';
import { formatWhatsApp } from './format.mjs';
import { approvedGroup, canReply, extractMessage, normalize, phoneOf, resolvePhone } from './policy.mjs';
import { saveAttachment, discardAttachment } from './media.mjs';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
if (existsSync(resolve(root, '.env'))) loadEnvFile(resolve(root, '.env'));
const teamPath = resolve(root, process.env.PLUME_TEAM_CONFIG_PATH ?? 'data/team.json');
const team = JSON.parse(readFileSync(teamPath, 'utf8'));
if (!/^[1-9][0-9]{7,14}$/.test(team.owner_phone) || !team.group_name) throw new Error('Invalid team configuration');
const token = process.env.PLUME_BRIDGE_TOKEN;
if (!token || token.length < 32) throw new Error('Run the local setup command first');
const backend = new URL(process.env.PLUME_BACKEND_URL ?? 'http://127.0.0.1:8000');
if (!['127.0.0.1', 'localhost'].includes(backend.hostname) || backend.protocol !== 'http:') {
  throw new Error('This pilot requires a loopback backend URL');
}
const dataDir = resolve(root, 'data/whatsapp');
const incomingDir = resolve(root, process.env.PLUME_INCOMING_PATH ?? 'data/incoming');
mkdirSync(dataDir, { recursive: true });
const store = new Store(resolve(dataDir, 'session.sqlite3'));
const auth = store.authState();
const logger = pino({ level: 'silent' }); // Never log message envelopes, keys or QR strings.
let status = 'Starting WhatsApp connection';
let qrImage = null;
let qrGeneration = 0;
let socket = null;
let connected = false;
let stopping = false;
let reconnectTimer;
let reconnectAttempt = 0;
let inboundPending = 0;
let inboundChain = Promise.resolve();
let processing = false;
const safeStatus = value => { status = value; console.log(value); };
const botIds = () => new Set([socket?.user?.id, socket?.user?.lid, auth.state.creds.me?.id, auth.state.creds.me?.lid]
  .filter(Boolean).map(normalize));

async function allowedGroup(chat) {
  try {
    const metadata = await socket.groupMetadata(chat);
    return approvedGroup(metadata, botIds());
  } catch {
    return true;
  }
}

async function accessPolicy() {
  const response = await fetch(new URL('/internal/whatsapp/policy', backend), {
    headers: { Authorization: `Bearer ${token}` }, signal: AbortSignal.timeout(5000),
  });
  if (!response.ok) throw new Error('Authorization unavailable');
  const policy = await response.json();
  if (!['public', 'whitelist', 'owner'].includes(policy.mode) || !Array.isArray(policy.members)) {
    throw new Error('Invalid policy');
  }
  return policy;
}

async function notificationState(id, state) {
  const url = new URL('/internal/whatsapp/notifications/' + encodeURIComponent(id), backend);
  const r = await fetch(url, { method: state ? 'POST' : 'GET',
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    body: state ? JSON.stringify({ state }) : undefined, signal: AbortSignal.timeout(5000) });
  if (!r.ok) throw new Error('Notification state unavailable');
  return r.json();
}
let pollingNotifications = false;
async function pollNotifications() {
  if (!connected || stopping || pollingNotifications) return;
  pollingNotifications = true;
  try {
    const r = await fetch(new URL('/internal/whatsapp/notifications', backend), {
      headers: { Authorization: `Bearer ${token}` }, signal: AbortSignal.timeout(5000) });
    if (!r.ok) return;
    for (const n of (await r.json()).notifications ?? []) {
      const id = 'notification:' + n.id;
      if (!store.hasJob(id)) store.enqueue(id, n.chat, {
        notification_id: n.id, notification_text: n.body, sender_phone: team.owner_phone,
        channel: n.chat.endsWith('@g.us') ? 'group' : 'dm', kind: 'text', chat_id: n.chat, event_id: id, text: n.body,
      });
      const row = store.db.prepare('SELECT state FROM jobs WHERE id=?').get(id);
      if (row) await notificationState(n.id, ['sent','uncertain'].includes(row.state) ? row.state : row.state === 'blocked' ? 'cancelled' : 'queued');
    }
  } catch { /* Keep persisted notifications for the next poll. */ }
  finally { pollingNotifications = false; }
}

async function selectGroup() {
  if (team.group_id) {
    safeStatus(await allowedGroup(team.group_id)
      ? 'Connected. Pinned group and DMs are ready for approved senders.'
      : 'Connected. Bot membership in the pinned group could not be verified.');
    return;
  }
  const groups = Object.values(await socket.groupFetchAllParticipating());
  const matches = groups.filter(group => group.subject === team.group_name);
  if (matches.length !== 1) {
    safeStatus('Connected. Test group missing or ambiguous; DMs are available.'); return;
  }
  const group = matches[0];
  if (!approvedGroup(group, botIds())) {
    safeStatus('Connected. Bot membership in the group could not be verified.'); return;
  }
  team.group_id = group.id;
  writeFileSync(teamPath + '.tmp', JSON.stringify(team, null, 2), { mode: 0o600 });
  renameSync(teamPath + '.tmp', teamPath);
  safeStatus('Connected. Pinned group and DMs are ready for approved senders.');
}

async function syncGroups() {
  if (!connected || !socket) return;
  try {
    const participating = await socket.groupFetchAllParticipating();
    const groups = Object.values(participating).map(g => ({
      chat: g.id,
      name: g.subject || g.id,
    }));
    if (groups.length > 0) {
      await fetch(new URL('/internal/whatsapp/groups/sync', backend), {
        method: 'POST',
        headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({ groups }),
        signal: AbortSignal.timeout(10000),
      });
    }
  } catch {}
}

async function receive(message) {
  if (!connected || message.key?.fromMe) return;
  const key = message.key ?? {};
  const group = key.remoteJid?.endsWith('@g.us');

  const sender = await resolvePhone(group ? key.participant : key.remoteJid,
    group ? key.participantAlt : key.remoteJidAlt, socket.signalRepository.lidMapping);
  let policy = await accessPolicy();
  if (group && !policy.groups?.includes(key.remoteJid)) {
    await syncGroups();
    policy = await accessPolicy();
  }
  const payload = extractMessage(message, {
    groupId: team.group_id, policy, senderPhone: sender,
    botIds: botIds(), isOurReply: (chat, id) => store.isOurReply(chat, id),
  });
  if (!payload || (group && !await allowedGroup(key.remoteJid))) return;
  const eventId = `${key.remoteJid}:${sender}:${key.id}`;
  if (store.hasJob(eventId)) return;
  if (payload.kind === 'document') {
    const document = normalizeMessageContent(message.message).documentMessage;
    payload.filename = String(document.fileName ?? 'attachment').slice(0, 200);
    Object.assign(payload, await saveAttachment(incomingDir, eventId, () =>
      downloadMediaMessage(message, 'stream', { options: { signal: AbortSignal.timeout(45000) } },
        { logger, reuploadRequest: socket.updateMediaMessage }), document.fileLength));
  }
  payload.raw_message = {
    key: {
      remoteJid: key.remoteJid,
      id: key.id,
      fromMe: false,
      participant: key.participant,
    },
    message: message.message,
  };
  if (!store.enqueue(eventId, key.remoteJid, payload)) {
    await discardAttachment(incomingDir, payload.file_token);
  }
}

async function work() {
  if (processing || !connected || stopping) return;
  const job = store.next();
  if (!job) return;
  processing = true;
  try {
    const payload = JSON.parse(job.payload);
    const policy = await accessPolicy();
    if (!canReply(policy, payload.sender_phone, job.chat, payload.channel === 'group') ||
        (payload.channel === 'group' && !await allowedGroup(job.chat))) {
      store.state(job.id, 'blocked');
      await discardAttachment(incomingDir, payload.file_token); return;
    }
    if (payload.notification_id) {
      const permission = await notificationState(payload.notification_id);
      if (!permission.allowed) {
        if (policy.paused) store.db.prepare('UPDATE jobs SET next_attempt=? WHERE id=?').run(Date.now() + 15000, job.id);
        if (!policy.paused) { store.state(job.id, 'blocked'); await notificationState(payload.notification_id, 'cancelled'); }
        return;
      }
      if (job.state === 'queued') { store.ready(job.id, payload.notification_text); return; }
    }
    if (job.state === 'queued') {
      const { raw_message, ...backendPayload } = payload;
      const response = await fetch(new URL('/internal/whatsapp/messages', backend), {
        method: 'POST', headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
        body: JSON.stringify(backendPayload), signal: AbortSignal.timeout(135000),
      });
      if (response.status === 401 || response.status === 403) {
        store.state(job.id, 'blocked'); await discardAttachment(incomingDir, payload.file_token);
        safeStatus('A message was blocked by backend authorization.'); return;
      }
      if (response.status === 429 && !response.headers.has('Retry-After')) {
        store.ready(job.id, 'The daily usage limit has been reached. It resets at 00:00 UTC.'); return;
      }
      if (!response.ok) { store.retry(job); return; }
      const result = await response.json();
      if (typeof result.text !== 'string' || !result.text.trim() || result.text.length > 64000) {
        store.state(job.id, 'failed'); return;
      }
      store.ready(job.id, result.text);
      await discardAttachment(incomingDir, payload.file_token);
      return;
    }
    if (!connected || (payload.channel === 'group' && !await allowedGroup(job.chat))) return;
    // A crash after sending is ambiguous. Never automatically replay that send.
    store.state(job.id, 'sending');
    try {
      let sent;
      try {
        const sendOptions = payload.raw_message ? { quoted: payload.raw_message } : {};
        sent = await socket.sendMessage(job.chat, { text: formatWhatsApp(job.reply) }, sendOptions);
      } catch (err) {
        if (payload.raw_message) {
          sent = await socket.sendMessage(job.chat, { text: formatWhatsApp(job.reply) });
        } else {
          throw err;
        }
      }
      if (!sent?.key?.id) throw new Error('Missing outbound ID');
      store.state(job.id, 'sent', sent.key.id);
      if (payload.notification_id) { try { await notificationState(payload.notification_id, 'sent'); } catch {} }
    } catch { store.state(job.id, 'uncertain'); safeStatus('Delivery uncertain; automatic resend suppressed.'); }
  } catch { store.retry(job); }
  finally { processing = false; }
}

function connect() {
  if (stopping) return;
  const current = makeWASocket({
    auth: auth.state, logger, markOnlineOnConnect: false,
    syncFullHistory: false, shouldSyncHistoryMessage: () => false,
    getMessage: async () => undefined,
  });
  socket = current;
  current.ev.on('creds.update', () => auth.saveCreds());
  current.ev.on('connection.update', update => {
    if (socket !== current) return;
    if (update.qr) {
      const qr = update.qr;
      const generation = ++qrGeneration;
      QRCode.toBuffer(qr, { type: 'png', width: 360 })
        .then(image => { if (generation === qrGeneration && !connected) qrImage = image; })
        .catch(() => safeStatus('QR rendering failed; waiting for another pairing code.'));
      safeStatus('Scan the QR code at http://127.0.0.1:8787 from the BOT account’s Linked devices.');
    }
    if (update.connection === 'open') {
      connected = true; qrImage = null; qrGeneration++; reconnectAttempt = 0;
      if (phoneOf(current.user?.id) === team.owner_phone) {
        safeStatus('The admin account was linked. Stop and relink using the separate bot account.');
        stopping = true; connected = false; current.end(undefined); return;
      }
      safeStatus('WhatsApp connected. Checking test group.');
      selectGroup().catch(() => safeStatus('Connected; group lookup failed. Restart to retry.'));
      syncGroups().catch(() => {});
    }
    if (update.connection === 'close') {
      connected = false; qrImage = null; qrGeneration++;
      const code = update.lastDisconnect?.error?.output?.statusCode;
      if (code === DisconnectReason.loggedOut || code === DisconnectReason.connectionReplaced) {
        safeStatus('WhatsApp logged out or session replaced. Stop and relink deliberately.'); return;
      }
      if (!stopping) {
        const delay = code === DisconnectReason.restartRequired ? 1000 :
          Math.min(60000, 2000 * 2 ** Math.min(reconnectAttempt++, 5)) + Math.floor(Math.random() * 1000);
        safeStatus('WhatsApp disconnected; reconnect scheduled.');
        clearTimeout(reconnectTimer);
        reconnectTimer = setTimeout(connect, delay);
      }
    }
  });
  current.ev.on('messages.upsert', ({ type, messages }) => {
    if (type !== 'notify') return;
    for (const message of messages) {
      if (inboundPending >= 50) break;
      inboundPending++;
      inboundChain = inboundChain.then(() => receive(message))
        .catch(() => safeStatus('A message could not be processed.'))
        .finally(() => { inboundPending--; });
    }
  });
  current.ev.on('groups.update', () => syncGroups().catch(() => {}));
  current.ev.on('group-participants.update', () => syncGroups().catch(() => {}));
}

const page = `<!doctype html><html lang="en"><meta charset="utf-8"><meta http-equiv="refresh" content="8">
<title>Nedal’s Smart Assistant — WhatsApp pairing</title><body>
<h1>Nedal’s Smart Assistant</h1><p>On the bot phone: WhatsApp → Settings → Linked devices → Link a device.</p>
<p>Scan a pairing QR using the bot account, not your personal admin account.</p>PAIRING_IMAGE
<p id="status">STATUS</p><p>This page stays on this computer. Refreshes every eight seconds.</p></body></html>`;
const server = createServer(async (request, response) => {
  if (!['127.0.0.1:8787', 'localhost:8787'].includes(request.headers.host) || request.method !== 'GET') {
    response.writeHead(403).end(); return;
  }
  response.setHeader('Cache-Control', 'no-store');
  response.setHeader('X-Content-Type-Options', 'nosniff');
  response.setHeader('Content-Security-Policy', "default-src 'none'; img-src 'self'; frame-ancestors 'none'");
  if (request.url === '/diagnostics') {
    const provided = Buffer.from(request.headers.authorization ?? '');
    const expected = Buffer.from(`Bearer ${token}`);
    if (provided.length !== expected.length || !timingSafeEqual(provided, expected)) {
      response.writeHead(401).end(); return;
    }
    try {
      const details = { connected, groupPinned: Boolean(team.group_id), groupAccessible: false };
      if (connected && team.group_id) {
        try {
          const group = await socket.groupMetadata(team.group_id);
          const livePolicy = await accessPolicy();
          const phones = new Set(livePolicy.members.filter(m => m.allowed && !m.blocked).map(m => m.phone));
          Object.assign(details, {
            groupAccessible: true, configuredNameMatches: group.subject === team.group_name,
            participantCount: group.participants?.length ?? 0,
            linkedToCommunity: Boolean(group.linkedParent),
            isCommunity: Boolean(group.isCommunity),
            isCommunityAnnouncement: Boolean(group.isCommunityAnnounce),
            onlyAdminsCanPost: Boolean(group.announce),
            approved: approvedGroup(group, botIds()),
            participants: await Promise.all((group.participants ?? []).map(async member => {
              const isBot = [member.id, member.phoneNumber, member.lid]
                .filter(Boolean).some(id => botIds().has(normalize(id)));
              const phone = isBot ? null : await resolvePhone(member.id, member.phoneNumber, socket.signalRepository.lidMapping);
              return { access: isBot ? 'bot' : phone === team.owner_phone ? 'owner' : phones.has(phone) ? 'member' : 'unapproved',
                phoneSuffix: !isBot && phone !== team.owner_phone ? phone?.slice(-4) ?? null : null,
                identityType: normalize(member.id).split('@')[1], role: member.admin ?? 'member' };
            })),
          });
        } catch { details.groupLookupFailed = true; }
      }
      response.writeHead(200, { 'Content-Type': 'application/json' }).end(JSON.stringify(details));
    } catch { response.writeHead(503).end(); }
    return;
  }
  if (request.url === '/') response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' }).end(
    page.replace('STATUS', status).replace('PAIRING_IMAGE', qrImage
      ? '<img src="/qr.png" width="360" height="360" alt="Scan with the bot phone">'
      : '<p>No pairing QR is currently required or available. See connection status below.</p>'));
  else if (request.url === '/qr.png' && qrImage) response.writeHead(200, { 'Content-Type': 'image/png' }).end(qrImage);
  else response.writeHead(404).end();
});
server.on('error', () => { console.error('Pairing port is unavailable; another bridge may be running.'); process.exit(1); });
// Bind before connecting, so starting two bridge instances cannot share the login.
server.listen(8787, '127.0.0.1', connect);
const worker = setInterval(() => { void work(); }, 1000);
const notificationsTimer = setInterval(() => { void pollNotifications(); }, 5000);
const cleanup = setInterval(() => store.cleanup(), 3600000);
store.cleanup();
function stop() {
  stopping = true; connected = false;
  clearTimeout(reconnectTimer); clearInterval(worker); clearInterval(cleanup); clearInterval(notificationsTimer);
  server.close(); socket?.end(undefined);
  // Keep auth and ambiguous sends on disk. No logout on shutdown.
  setTimeout(() => process.exit(0), 1000).unref();
}
process.on('SIGINT', stop);
process.on('SIGTERM', stop);
process.on('uncaughtException', () => { console.error('Bridge stopped after an internal error.'); process.exit(1); });
process.on('unhandledRejection', () => { console.error('Bridge stopped after an asynchronous error.'); process.exit(1); });
