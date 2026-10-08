import { jidNormalizedUser, normalizeMessageContent } from '@whiskeysockets/baileys';

export const normalize = jid => jidNormalizedUser(jid ?? '');
export const phoneOf = jid => {
  const match = /^([1-9][0-9]{7,14})@s\.whatsapp\.net$/.exec(normalize(jid));
  return match?.[1] ?? null;
};

export async function resolvePhone(primary, alternate, mapping) {
  const direct = phoneOf(primary);
  if (direct) return direct;
  if (!normalize(primary).endsWith('@lid')) return null;
  // Alternate identity comes from the decoded WhatsApp envelope, never message text.
  return phoneOf(alternate) ?? phoneOf(await mapping.getPNForLID(normalize(primary)));
}

export function canReply(policy, phone, chat, group) {
  if (!phone) return false;
  if (phone === policy.owner) return true;
  const member = policy.members?.find(m => m.phone === phone);
  if (policy.paused || member?.blocked || policy.mode === 'owner') return false;
  if (policy.mode === 'public') return true;
  return Boolean(member?.allowed && (!group || policy.groups?.includes(chat)));
}

export function extractMessage(message, { groupId, approvedPhones, policy, senderPhone, botIds, isOurReply, now = Date.now() }) {
  const key = message.key ?? {};
  if (key.fromMe || !key.id || !key.remoteJid) return null;
  const group = key.remoteJid.endsWith('@g.us');
  if (policy ? !canReply(policy, senderPhone, key.remoteJid, group) : !approvedPhones?.has(senderPhone)) return null;
  if (!policy && group && key.remoteJid !== groupId) return null;
  if (!group && !/@(s\.whatsapp\.net|lid)$/.test(key.remoteJid)) return null;
  const timestamp = Number(message.messageTimestamp) * 1000;
  if (!Number.isFinite(timestamp) || timestamp > now + 60000 || timestamp < now - 600000) return null;
  const content = normalizeMessageContent(message.message);
  if (!content) return null;
  const document = content.documentMessage;
  const extended = content.extendedTextMessage;
  const text = content.conversation ?? extended?.text ?? document?.caption ?? '';
  const context = extended?.contextInfo ?? document?.contextInfo;
  const mentioned = (context?.mentionedJid ?? []).some(jid => botIds.has(normalize(jid)));
  const replied = isOurReply(key.remoteJid, context?.stanzaId);
  const commanded = /^\/(assistant|nedal|plume|admin|about|help|status|remember|replace|forget|memories|history|busy|schedule|timezone|availability|calendar|confirm)(?:\s|$)/i.test(text);
  // Documents are the explicitly requested exception to the quiet-group rule.
  if (group && policy?.group_trigger !== 'all' && !mentioned && !replied && !commanded && !document) return null;
  if (!document && (!text.trim() || text.length > 8000)) return null;
  return {
    event_id: key.id, sender_phone: senderPhone, chat_id: key.remoteJid,
    channel: group ? 'group' : 'dm', kind: document ? 'document' : 'text',
    text: text.slice(0, 8000),
  };
}

export function approvedGroup(metadata, botIds) {
  // Other participants do not grant or block access. Each sender is authorized separately.
  return (metadata.participants ?? []).some(member =>
    [member.id, member.phoneNumber, member.lid].filter(Boolean)
      .some(id => botIds.has(normalize(id))));
}
