/**
 * WhatsApp bridge (Baileys) — intentionally minimal.
 *
 * This is the ONLY piece of the app still in Node.js. Baileys implements
 * WhatsApp's multi-device protocol (Noise handshake + Signal encryption)
 * and has no maintained Python equivalent, so we keep it here as a thin
 * connector and do EVERYTHING else (dashboard, DB, AI, calendar) in Python.
 *
 * This process:
 *   - opens/maintains Baileys sessions (QR pairing, reconnects, history sync)
 *   - forwards every event to the Python app via webhook POSTs
 *   - exposes a tiny HTTP API so Python can start/stop sessions and send messages
 *
 * It holds NO business logic, NO database, and NO AI calls — Python owns all of that.
 */
require('dotenv').config();
const path = require('path');
const fs = require('fs/promises');
const express = require('express');
const QRCode = require('qrcode');
const pino = require('pino');
const {
  default: makeWASocket,
  useMultiFileAuthState,
  fetchLatestBaileysVersion,
  DisconnectReason,
} = require('@whiskeysockets/baileys');

const AUTH_DIR = path.join(__dirname, 'auth_sessions');
const BRIDGE_PORT = process.env.BRIDGE_PORT || 4000;

// When Python sends a message through /send, WhatsApp echoes it straight
// back through messages.upsert with fromMe:true — same as a human typing
// from their own phone. We remember what the bot just sent (per chat, for
// a few seconds) so that echo isn't mistaken for a real human takeover.
const recentBotSent = new Map(); // "accountId:jid" -> Set<text>

function rememberBotSent(accountId, jid, text) {
  const key = `${accountId}:${jid}`;
  if (!recentBotSent.has(key)) recentBotSent.set(key, new Set());
  recentBotSent.get(key).add(text);
  setTimeout(() => {
    recentBotSent.get(key)?.delete(text);
  }, 30_000);
}

function wasJustSentByBot(accountId, jid, text) {
  const key = `${accountId}:${jid}`;
  const set = recentBotSent.get(key);
  if (set && set.has(text)) {
    set.delete(text);
    return true;
  }
  return false;
}

const PYTHON_WEBHOOK_URL = process.env.PYTHON_WEBHOOK_URL || 'http://localhost:8000/webhook';

const app = express();
app.use(express.json());

const sockets = new Map(); // accountId -> live baileys socket
const groupNameCache = new Map();

// ---------------------------------------------------------------------------
// LID <-> phone-number resolution for group participants.
//
// Since Baileys 6.7.19, WhatsApp's "LID" (Linked/hidden ID) privacy mode
// means a group's participants often arrive as opaque "...@lid" ids (see
// meta.participants[].id) instead of a phone-number jid
// ("91xxxxxxxxxx@s.whatsapp.net"). When that's the case, Baileys *also*
// gives us meta.participants[].phoneNumber — the real phone-number jid
// paired with that lid — as long as WhatsApp included it in the group
// metadata response. We cache that lid->phone mapping per group so:
//   1. the "fetch members" dashboard screen can show the real number
//      instead of a meaningless internal id, and
//   2. every inbound group message can carry a resolved "senderPhone"
//      alongside the raw "senderJid" (which may be a lid), so Python can
//      match a client that was configured by phone number even when
//      WhatsApp is addressing this particular group by lid.
// ---------------------------------------------------------------------------
const participantPhoneCache = new Map(); // "accountId:groupJid" -> { at: number, map: Map(id -> phoneJid) }
const PARTICIPANT_CACHE_TTL_MS = 10 * 60 * 1000; // 10 minutes

// ---------------------------------------------------------------------------
// Display-name resolution for group participants.
//
// groupMetadata() (used by the /members endpoint below) gives us jids/lids
// but NOT a human-readable name — WhatsApp just doesn't include one there.
// The only place we ever legitimately learn someone's name is from a
// message they sent (msg.pushName) or from a contacts sync event. So we
// keep a small in-memory "whatever we've learned so far" cache per account
// and consult it when building the members list — this is exactly why the
// conversation view could already show "Subhakar Maurya" while "Fetch group
// members" only showed raw numbers/lids: messages carry pushName, group
// metadata doesn't. This cache just makes that same info available to the
// members list too. It resets on restart and fills back in as people send
// messages / as WhatsApp syncs contacts — there's no way to ask WhatsApp
// for a name we've never seen without the account holder's phone having
// synced that contact.
// ---------------------------------------------------------------------------
const contactNamesByAccount = new Map(); // accountId -> Map(jid -> name)

// Oldest message we've personally seen for each chat, so we have a
// reference point to ask WhatsApp for anything older (sock.fetchMessageHistory
// needs the current oldest message's key+timestamp — see rememberOldest /
// the /sync-history route below). "accountId:jid" -> { key, timestamp }
const oldestMsgByChat = new Map();
function rememberOldest(accountId, jid, key, timestamp) {
  if (!key || !timestamp) return;
  const cacheKey = `${accountId}:${jid}`;
  const cur = oldestMsgByChat.get(cacheKey);
  if (!cur || timestamp < cur.timestamp) {
    oldestMsgByChat.set(cacheKey, { key, timestamp });
  }
}

function rememberContactName(accountId, jid, name) {
  if (!jid || !name) return;
  if (!contactNamesByAccount.has(accountId)) contactNamesByAccount.set(accountId, new Map());
  contactNamesByAccount.get(accountId).set(jid, name);
}

function lookupContactName(accountId, ...jids) {
  const map = contactNamesByAccount.get(accountId);
  if (!map) return null;
  for (const jid of jids) {
    if (jid && map.has(jid)) return map.get(jid);
  }
  return null;
}

// Best-effort lid -> phone-number-jid resolution using Baileys' own Signal
// store (sock.signalRepository.lidMapping), which is built up over time from
// message/session traffic and is often populated even when a single
// groupMetadata() call doesn't hand us p.phoneNumber (that field is only
// filled in when WhatsApp happens to include the pairing in THAT response).
// Guarded because the method's presence/shape has changed across Baileys
// versions — if it's missing or throws, we just fall back to nothing.
async function resolvePhoneFromLidStore(sock, lid) {
  try {
    const lidMapping = sock.signalRepository?.lidMapping;
    if (!lidMapping || typeof lidMapping.getPNForLID !== 'function') return null;
    const pn = await lidMapping.getPNForLID(lid);
    if (!pn) return null;
    // Some Baileys versions return the bare number, others the full jid.
    return pn.includes('@') ? pn : `${pn}@s.whatsapp.net`;
  } catch (e) {
    return null;
  }
}

// Normalises ONE groupMetadata participant across Baileys versions/shapes.
// Depending on version and the group's addressingMode, the same person can
// arrive as { id: phoneJid, lid } (id = phone, lid separate), or as
// { id: lid, phoneNumber } — so we look at every field, pick out whichever
// one is a phone-number jid (@s.whatsapp.net) and whichever is a @lid, and
// only then fall back to Baileys' lid<->phone store.
// Returns { jid, lid, phoneNumber } where `jid` is what WhatsApp addresses
// this member as in this group (kept for matching incoming senders).
// lid -> phone-number jid pairs the admin confirmed by typing a number in the
// dashboard ("Find member by number"). Kept per account so later fetches show
// the number too.
const learnedLidPhone = new Map(); // accountId -> Map(lid -> phoneJid)

async function resolveParticipant(sock, p, accountId) {
  const fields = [p.id, p.phoneNumber, p.phone_number, p.jid, p.lid].filter((v) => typeof v === 'string' && v);
  let phoneNumber = fields.find((v) => v.endsWith('@s.whatsapp.net')) || null;
  const lid = fields.find((v) => v.endsWith('@lid')) || null;
  if (!phoneNumber && lid) phoneNumber = await resolvePhoneFromLidStore(sock, lid);
  if (!phoneNumber && lid && accountId) phoneNumber = learnedLidPhone.get(accountId)?.get(lid) || null;
  if (phoneNumber) phoneNumber = phoneNumber.replace(/:\d+@/, '@'); // strip device suffix
  if (!phoneNumber) {
    console.warn('[bridge] could not resolve phone for participant', JSON.stringify(p));
  }
  return { jid: p.id, lid, phoneNumber };
}

async function getParticipantPhoneMap(sock, accountId, groupJid, { forceRefresh = false } = {}) {
  const cacheKey = `${accountId}:${groupJid}`;
  const cached = participantPhoneCache.get(cacheKey);
  if (!forceRefresh && cached && Date.now() - cached.at < PARTICIPANT_CACHE_TTL_MS) {
    return cached.map;
  }
  try {
    const meta = await sock.groupMetadata(groupJid);
    const map = new Map();
    for (const p of meta.participants || []) {
      // p.id is whatever Baileys is currently addressing this participant
      // as (lid or phone-jid, depending on the group's addressingMode).
      // p.phoneNumber is only populated when p.id is a lid AND WhatsApp
      // sent the paired phone-number jid.
      const r = await resolveParticipant(sock, p, accountId);
      if (r.phoneNumber) {
        map.set(p.id, r.phoneNumber);
        if (r.lid) map.set(r.lid, r.phoneNumber);
      }
    }
    participantPhoneCache.set(cacheKey, { at: Date.now(), map });
    return map;
  } catch (e) {
    console.error(`[bridge] getParticipantPhoneMap failed for ${groupJid}:`, e.message);
    return cached?.map || new Map();
  }
}

async function postWebhook(pathname, payload) {
  try {
    await fetch(`${PYTHON_WEBHOOK_URL}${pathname}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
  } catch (e) {
    console.error(`[bridge] webhook ${pathname} failed:`, e.message);
  }
}

function extractText(msg) {
  return (
    msg.message?.conversation ||
    msg.message?.extendedTextMessage?.text ||
    msg.message?.imageMessage?.caption ||
    ''
  );
}

async function getGroupName(sock, accountId, jid, hint) {
  const cacheKey = `${accountId}:${jid}`;
  if (groupNameCache.has(cacheKey)) return groupNameCache.get(cacheKey);
  if (hint) {
    groupNameCache.set(cacheKey, hint);
    return hint;
  }
  try {
    const meta = await sock.groupMetadata(jid);
    const name = meta?.subject || jid;
    groupNameCache.set(cacheKey, name);
    return name;
  } catch (e) {
    return jid;
  }
}

/**
 * Starts (or restarts) a Baileys session for accountId. Every connection
 * event is simply forwarded to Python as a webhook — Python decides what
 * to persist (accounts/contacts/messages tables) and what to do about it
 * (e.g. calling OpenAI for a reply).
 */
async function createSession(accountId) {
  const { state, saveCreds } = await useMultiFileAuthState(path.join(AUTH_DIR, accountId));
  const { version } = await fetchLatestBaileysVersion();

  const sock = makeWASocket({
    version,
    auth: state,
    logger: pino({ level: 'silent' }),
    browser: ['WhatsApp AI Bot', 'Chrome', '1.0.0'],
    syncFullHistory: true,
  });

  sockets.set(accountId, sock);
  sock.ev.on('creds.update', saveCreds);

  // WhatsApp periodically syncs the account holder's address book — this
  // is the main source of real names for people who haven't messaged yet.
  sock.ev.on('contacts.upsert', (contacts) => {
    for (const c of contacts || []) {
      rememberContactName(accountId, c.id, c.name || c.notify || c.verifiedName);
    }
  });
  sock.ev.on('contacts.update', (contacts) => {
    for (const c of contacts || []) {
      rememberContactName(accountId, c.id, c.name || c.notify || c.verifiedName);
    }
  });

  sock.ev.on('connection.update', async (update) => {
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      const dataUrl = await QRCode.toDataURL(qr);
      await postWebhook('/qr', { accountId, dataUrl });
      await postWebhook('/status', { accountId, status: 'pending_qr' });
    }

    if (connection === 'open') {
      const phone = sock.user?.id?.split(':')[0] || null;
      const name = sock.user?.name || sock.user?.notify || null;
      await postWebhook('/status', { accountId, status: 'connected', phone, name });
    }

    if (connection === 'close') {
      const statusCode = lastDisconnect?.error?.output?.statusCode;
      const loggedOut = statusCode === DisconnectReason.loggedOut;
      console.log(
        `[bridge ${accountId}] closed (statusCode=${statusCode}, loggedOut=${loggedOut}):`,
        lastDisconnect?.error?.message || lastDisconnect?.error
      );
      sockets.delete(accountId);

      if (loggedOut) {
        await postWebhook('/status', { accountId, status: 'disconnected', loggedOut: true });
        await fs.rm(path.join(AUTH_DIR, accountId), { recursive: true, force: true }).catch(() => {});
        return;
      }

      // statusCode 515 "restart required" fires right after a QR scan and
      // just means "reconnect once more to finish pairing" — always retry
      // with the same saved creds except on an explicit logout.
      await postWebhook('/status', { accountId, status: 'reconnecting' });
      createSession(accountId).catch((e) => console.error('[bridge] reconnect failed', e));
    }
  });

  // WhatsApp pushes existing chat history right after a successful link.
  sock.ev.on('messaging-history.set', async ({ chats, messages }) => {
    try {
      const phone = sock.user?.id?.split(':')[0] || null;
      const name = sock.user?.name || sock.user?.notify || null;

      const nameByJid = new Map();
      for (const chat of chats || []) {
        if (chat?.id && chat.name) nameByJid.set(chat.id, chat.name);
      }

      const items = [];
      for (const msg of messages || []) {
        if (!msg.message) continue;
        const jid = msg.key.remoteJid;
        if (!jid || jid === 'status@broadcast') continue;
        const isGroup = jid.endsWith('@g.us');
        const text = extractText(msg);
        if (!text) continue;

        const chatName = isGroup
          ? await getGroupName(sock, accountId, jid, nameByJid.get(jid))
          : nameByJid.get(jid) || msg.pushName || null;

        const fromMe = !!msg.key.fromMe;
        const senderJid = isGroup ? (msg.key.participant || (fromMe ? sock.user?.id : jid)) : jid;
        if (!fromMe && msg.pushName) rememberContactName(accountId, senderJid, msg.pushName);
        const ts = msg.messageTimestamp ? Number(msg.messageTimestamp) : null;
        rememberOldest(accountId, jid, msg.key, ts);

        items.push({
          jid,
          name: chatName,
          isGroup,
          direction: fromMe ? 'out' : 'in',
          body: text,
          messageId: msg.key.id || null,
          senderJid,
          senderName: msg.pushName || null,
          timestamp: ts,
        });
      }

      if (items.length) {
        await postWebhook('/history', { accountId, phone, name, items });
      }
    } catch (err) {
      console.error('[bridge] history import failed:', err.message);
    }
  });

  sock.ev.on('messages.upsert', async ({ messages, type }) => {
    if (type !== 'notify' && type !== 'append') return;

    for (const msg of messages) {
      if (!msg.message) continue;
      const jid = msg.key.remoteJid;
      if (jid === 'status@broadcast') continue;
      const isGroup = jid.endsWith('@g.us');
      const text = extractText(msg);
      if (!text) continue;

      // fromMe = the account holder typed this themselves from their own
      // phone/WhatsApp Web — Python needs to see these too, so it can tell
      // "the human took over this chat" apart from the bot's own replies.
      // But if the bot itself just sent this exact text via /send, it's
      // an echo of our own message, not a human takeover — skip it.
      const fromMe = !!msg.key.fromMe;
      if (fromMe && wasJustSentByBot(accountId, jid, text)) continue;

      const name = isGroup ? await getGroupName(sock, accountId, jid) : msg.pushName || null;

      // For a GROUP chat, remoteJid is the group's own jid — the actual
      // sender is msg.key.participant (set by WhatsApp on every group
      // message, including fromMe ones). For a 1:1 chat there's no
      // participant field; the sender IS the remoteJid.
      const senderJid = isGroup ? (msg.key.participant || (fromMe ? sock.user?.id : jid)) : jid;
      const senderName = fromMe ? (sock.user?.name || sock.user?.notify || null) : (msg.pushName || null);
      if (!fromMe && msg.pushName) rememberContactName(accountId, senderJid, msg.pushName);
      rememberOldest(accountId, jid, msg.key, msg.messageTimestamp ? Number(msg.messageTimestamp) : null);

      // Best-effort: resolve senderJid to its real phone-number jid, in case
      // this group addresses participants by lid (see getParticipantPhoneMap
      // above). Python's sender-role match then checks BOTH senderJid and
      // senderPhone, so a client configured by phone number still resolves
      // even when WhatsApp is only giving us their lid on this message.
      let senderPhone = null;
      if (isGroup && senderJid) {
        const phoneMap = await getParticipantPhoneMap(sock, accountId, jid);
        senderPhone = phoneMap.get(senderJid) || null;
      }

      await postWebhook('/message', {
        accountId,
        jid,
        name,
        isGroup,
        fromMe,
        body: text,
        // WhatsApp's own message id — used by Python for idempotency
        // (duplicate-webhook / duplicate-event protection).
        messageId: msg.key.id || null,
        // Who actually sent it. In a group this is the participant's jid
        // (e.g. "9198xxxxxxx@s.whatsapp.net", or an opaque "...@lid" if
        // this group addresses members by lid — see senderPhone below).
        senderJid,
        // The same sender's phone-number jid, resolved from group metadata
        // when senderJid is a lid. null if WhatsApp hasn't given us that
        // mapping yet (e.g. the member has never sent a message before).
        senderPhone,
        senderName,
        // Timestamp WhatsApp attached to the message (seconds since epoch).
        timestamp: msg.messageTimestamp ? Number(msg.messageTimestamp) : null,
        // If this message was a reply to another one, WhatsApp's own id
        // for the message being replied to (useful for reply-to threading).
        quotedMessageId:
          msg.message?.extendedTextMessage?.contextInfo?.stanzaId || null,
      });
    }
  });

  return sock;
}

// ---------- HTTP API for Python to control this bridge ----------

// Start (or restart) a WhatsApp session
app.post('/sessions/:accountId', async (req, res) => {
  createSession(req.params.accountId).catch((e) =>
    console.error('[bridge] createSession failed:', e.message)
  );
  res.json({ started: req.params.accountId });
});

// List the WhatsApp groups this session currently participates in — lets
// the dashboard show real group names/JIDs instead of the admin having to
// copy a raw "120363...@g.us" id out of WhatsApp manually.
app.get('/sessions/:accountId/groups', async (req, res) => {
  const sock = sockets.get(req.params.accountId);
  if (!sock) return res.status(409).json({ error: 'session not connected' });
  try {
    const groups = await sock.groupFetchAllParticipating();
    const list = Object.values(groups).map((g) => ({
      jid: g.id,
      name: g.subject,
      participantCount: (g.participants || []).length,
    }));
    res.json({ groups: list });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// Members of one specific group. `jid` is whatever WhatsApp is currently
// addressing this participant as for THIS group (a phone-number jid, or an
// opaque "...@lid" if the group uses lid-addressing for privacy).
// `phoneNumber` is the real phone-number jid when we can resolve it — that's
// always the field to show the admin and the field the dashboard stores for
// matching, since `jid` alone (when it's a lid) can't be typed/verified by
// a human and isn't necessarily stable if WhatsApp reassigns it. Used by
// "Add client group" so client/internal roles are assigned from the real
// roster instead of the admin hand-typing raw WhatsApp IDs (error-prone: a
// mistyped or manually-guessed id silently never matches a real sender).
app.get('/sessions/:accountId/groups/:groupJid/members', async (req, res) => {
  const sock = sockets.get(req.params.accountId);
  if (!sock) return res.status(409).json({ error: 'session not connected' });
  const groupJid = decodeURIComponent(req.params.groupJid);
  try {
    const meta = await sock.groupMetadata(groupJid);
    const members = await Promise.all((meta.participants || []).map(async (p) => {
      const r = await resolveParticipant(sock, p, req.params.accountId);
      return {
        jid: p.id,
        lid: r.lid,
        phoneNumber: r.phoneNumber,
        // Best-effort display name — null if this account has never been
        // given a name for them (never messaged / not in address book).
        name: lookupContactName(req.params.accountId, p.id, r.lid, r.phoneNumber),
        isAdmin: p.admin === 'admin' || p.admin === 'superadmin',
      };
    }));
    // Refresh the phone-map cache from this same metadata call so the very
    // next inbound message from these members can already resolve senderPhone.
    participantPhoneCache.set(`${req.params.accountId}:${groupJid}`, {
      at: Date.now(),
      map: new Map(members.filter((m) => m.phoneNumber).map((m) => [m.jid, m.phoneNumber])),
    });
    res.json({ members });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// "Find member by number": the admin types a client's phone number and we
// work out which participant of this group it is. Direction is number -> lid
// (the one direction WhatsApp actually lets us look up): ask WhatsApp for the
// number's jid/lid, then match against the group's participants.
app.post('/sessions/:accountId/groups/:groupJid/lookup-number', async (req, res) => {
  const accountId = req.params.accountId;
  const sock = sockets.get(accountId);
  if (!sock) return res.status(409).json({ error: 'session not connected' });
  const groupJid = decodeURIComponent(req.params.groupJid);
  const digits = String(req.body?.number || '').replace(/[^0-9]/g, '');
  if (digits.length < 8) return res.status(400).json({ error: 'enter the number with country code, e.g. 919082470614' });
  const phoneJid = `${digits}@s.whatsapp.net`;
  try {
    let lid = null;
    let exists = true;
    try {
      const r = await sock.onWhatsApp(phoneJid);
      const hit = Array.isArray(r) ? r[0] : null;
      if (!hit || hit.exists === false) exists = false;
      else lid = hit.lid || null;
    } catch (e) {
      console.warn('[bridge] onWhatsApp failed:', e.message);
    }
    if (!exists) return res.json({ found: false, reason: 'not_on_whatsapp' });
    if (!lid) {
      try {
        lid = (await sock.signalRepository?.lidMapping?.getLIDForPN?.(phoneJid)) || null;
      } catch (e) { /* ignore */ }
    }
    const meta = await sock.groupMetadata(groupJid);
    const norm = (v) => (typeof v === 'string' ? v.replace(/:\d+@/, '@') : v);
    const match = (meta.participants || []).find((p) =>
      [p.id, p.phoneNumber, p.phone_number, p.jid, p.lid].map(norm).some((v) => v && (v === phoneJid || (lid && v === lid)))
    );
    if (!match) {
      return res.json({ found: false, reason: lid ? 'not_in_group' : 'cannot_link', lid });
    }
    const memberLid = [match.id, match.lid, match.jid].find((v) => typeof v === 'string' && v.endsWith('@lid')) || lid || null;
    if (memberLid) {
      if (!learnedLidPhone.has(accountId)) learnedLidPhone.set(accountId, new Map());
      learnedLidPhone.get(accountId).set(memberLid, phoneJid);
    }
    res.json({ found: true, member: { jid: match.id, lid: memberLid, phoneNumber: phoneJid } });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// Send a message out through a linked session
// Best-effort "get me more of this chat's backlog". WhatsApp only lets you
// ask for OLDER messages than one you already have (sock.fetchMessageHistory
// needs a reference key+timestamp — see rememberOldest above), so this can
// only pull further back from whatever we've already seen; it can't
// materialise history for a chat we've never observed a single message in.
// Results don't come back from this call — they arrive later through the
// normal 'messaging-history.set' listener -> POST /webhook/history, same as
// the history WhatsApp pushes automatically after linking.
app.post('/sessions/:accountId/groups/:groupJid/sync-history', async (req, res) => {
  const accountId = req.params.accountId;
  const sock = sockets.get(accountId);
  if (!sock) return res.status(409).json({ error: 'session not connected' });
  const groupJid = decodeURIComponent(req.params.groupJid);
  const pages = Math.min(Math.max(parseInt(req.body?.pages, 10) || 3, 1), 10);
  const cacheKey = `${accountId}:${groupJid}`;

  if (!oldestMsgByChat.has(cacheKey)) {
    return res.json({ requested: false, reason: 'no_reference' });
  }

  res.json({ requested: true, pages }); // don't make the admin's "Save group" wait on WhatsApp
  (async () => {
    for (let i = 0; i < pages; i++) {
      const ref = oldestMsgByChat.get(cacheKey);
      if (!ref) break;
      try {
        await sock.fetchMessageHistory(50, ref.key, ref.timestamp);
      } catch (e) {
        console.warn(`[bridge] fetchMessageHistory failed for ${groupJid}:`, e.message);
        break;
      }
      // Give the resulting messaging-history.set event time to land (and
      // update oldestMsgByChat) before asking for the next page back.
      await new Promise((r) => setTimeout(r, 2500));
      const next = oldestMsgByChat.get(cacheKey);
      if (!next || next.timestamp === ref.timestamp) break; // no further progress
    }
  })().catch((e) => console.error('[bridge] sync-history background loop failed:', e.message));
});

app.post('/sessions/:accountId/send', async (req, res) => {
  const sock = sockets.get(req.params.accountId);
  if (!sock) return res.status(409).json({ error: 'session not connected' });
  const { jid, text } = req.body;
  try {
    await sock.sendMessage(jid, { text });
    rememberBotSent(req.params.accountId, jid, text);
    res.json({ sent: true });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// Fully unlink a session: logout on WhatsApp's side + wipe local auth files
app.delete('/sessions/:accountId', async (req, res) => {
  const accountId = req.params.accountId;
  const sock = sockets.get(accountId);
  if (sock) {
    try {
      await sock.logout();
    } catch (e) {
      /* already disconnected — fine */
    }
    try {
      sock.end(undefined);
    } catch (e) {
      /* ignore */
    }
    sockets.delete(accountId);
  }
  await fs.rm(path.join(AUTH_DIR, accountId), { recursive: true, force: true }).catch(() => {});
  res.json({ removed: accountId });
});

// ---------- boot: restore any sessions that already have saved auth ----------

async function restoreAllSessions() {
  await fs.mkdir(AUTH_DIR, { recursive: true });
  const entries = await fs.readdir(AUTH_DIR, { withFileTypes: true });
  for (const entry of entries) {
    if (entry.isDirectory()) {
      createSession(entry.name).catch((e) =>
        console.error(`[bridge] failed to restore ${entry.name}:`, e.message)
      );
    }
  }
}

restoreAllSessions()
  .then(() => {
    app.listen(BRIDGE_PORT, () =>
      console.log(`[bridge] WhatsApp connector running on http://localhost:${BRIDGE_PORT}`)
    );
  })
  .catch((err) => {
    console.error('[bridge] failed to start:', err);
    process.exit(1);
  });
