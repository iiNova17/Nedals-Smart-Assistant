import { normalizeMessageContent } from '@whiskeysockets/baileys';
import { formatWhatsApp } from './format.mjs';

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

export async function sendReply(socket, chat, text, rawMessage) {
  const quote = quoteFor(rawMessage);
  const options = quote?.key.remoteJid === chat ? { quoted: quote } : {};
  // Once sending starts, any failure may mean delivery happened. Never retry unquoted.
  return socket.sendMessage(chat, { text: formatWhatsApp(text) }, options);
}
