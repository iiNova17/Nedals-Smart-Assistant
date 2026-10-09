import { normalizeMessageContent } from '@whiskeysockets/baileys';
import { formatWhatsApp } from './format.mjs';
import { phoneOf, normalize } from './policy.mjs';

// Persist only what a text quote needs, never media keys or an entire nested envelope.
export function quoteFor(message) {
  const key = message?.key;
  if (!key?.id || !key.remoteJid) return undefined;
  const content = normalizeMessageContent(message.message) ?? {};
  const text = content.conversation ?? content.extendedTextMessage?.text ??
    content.documentMessage?.caption ?? content.documentMessage?.fileName ?? '[Message]';
  return {
    key: { remoteJid: key.remoteJid, id: key.id, fromMe: Boolean(key.fromMe),
      ...(key.participant ? { participant: key.participant } : {}) },
    message: { conversation: String(text).slice(0, 8000) },
  };
}

export function extractReplyContext(message) {
  const content = normalizeMessageContent(message?.message) ?? {};
  const context =
    content.extendedTextMessage?.contextInfo ??
    content.documentMessage?.contextInfo ??
    content.audioMessage?.contextInfo ??
    content.stickerMessage?.contextInfo ??
    content.imageMessage?.contextInfo ??
    content.videoMessage?.contextInfo;

  if (!context?.stanzaId) return null;

  const quotedRaw = context.quotedMessage;
  const quoted = normalizeMessageContent(quotedRaw) ?? {};

  let contentType = 'unknown';
  let textOrCaption = '';
  let hasAttachment = false;

  if (quoted.conversation || quoted.extendedTextMessage) {
    contentType = 'text';
    textOrCaption = quoted.conversation ?? quoted.extendedTextMessage?.text ?? '';
  } else if (quoted.documentMessage) {
    contentType = 'document';
    textOrCaption = quoted.documentMessage.caption ?? quoted.documentMessage.fileName ?? '[Document]';
    hasAttachment = true;
  } else if (quoted.audioMessage) {
    contentType = 'audio';
    textOrCaption = quoted.audioMessage.ptt ? '[Voice note]' : '[Audio]';
    hasAttachment = true;
  } else if (quoted.stickerMessage) {
    contentType = 'sticker';
    textOrCaption = '[Sticker]';
    hasAttachment = true;
  } else if (quoted.imageMessage) {
    contentType = 'image';
    textOrCaption = quoted.imageMessage.caption ?? '[Image]';
    hasAttachment = true;
  } else if (quoted.videoMessage) {
    contentType = 'video';
    textOrCaption = quoted.videoMessage.caption ?? '[Video]';
  } else if (!quotedRaw || Object.keys(quoted).length === 0) {
    contentType = 'unavailable';
    textOrCaption = '[Quoted content unavailable]';
  }

  const authorJid = context.participant ? normalize(context.participant) : '';
  const authorPhone = phoneOf(authorJid);

  return {
    message_id: context.stanzaId,
    chat_id: message?.key?.remoteJid ?? '',
    author_identifier: authorPhone || authorJid,
    content_type: contentType,
    text_or_caption: String(textOrCaption).slice(0, 8000),
    has_attachment: hasAttachment,
    quoted_raw: quotedRaw,
    unavailable: contentType === 'unavailable',
    media_error: '',
    attachment_reference: '',
  };
}

export async function sendReply(socket, chat, text, rawMessage, mentions = []) {
  const quote = quoteFor(rawMessage);
  const options = quote?.key.remoteJid === chat ? { quoted: quote } : {};
  // Once sending starts, any failure may mean delivery happened. Never retry unquoted.
  const safeMentions = chat.endsWith('@g.us') && Array.isArray(mentions)
    ? [...new Set(mentions.filter(jid => /^[1-9][0-9]{7,14}@s\.whatsapp\.net$/.test(jid)))].slice(0, 20) : [];
  return socket.sendMessage(chat, { text: formatWhatsApp(text),
    ...(safeMentions.length ? { mentions: safeMentions } : {}) }, options);
}
