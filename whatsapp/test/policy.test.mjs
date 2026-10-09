import test from 'node:test';
import assert from 'node:assert/strict';
import { extractMessage, resolvePhone, approvedGroup, canReply } from '../src/policy.mjs';

const owner = '15555550100';
const bot = '15555550101@s.whatsapp.net';
const mapping = { getPNForLID: async lid => lid === '99@lid' ? `${owner}@s.whatsapp.net` : null };
const options = { groupId: '123@g.us', ownerPhone: owner, senderPhone: owner,
  approvedPhones: new Set([owner, '15555550200']),
  botIds: new Set([bot]), isOurReply: (_, id) => id === 'our-message' };
const message = content => ({ key: { id: 'm1', remoteJid: '123@g.us', participant: `${owner}@s.whatsapp.net` },
  messageTimestamp: Math.floor(Date.now() / 1000), message: content });

test('images retain captions and mentions while ordinary group photos stay quiet', () => {
  assert.equal(extractMessage(message({imageMessage: {caption: 'Look at this'}}), options), null);
  const captioned = extractMessage(message({imageMessage: {caption: 'Describe this',
    contextInfo: {mentionedJid: [bot]}}}), options);
  assert.equal(captioned.kind, 'image');
  assert.equal(captioned.text, 'Describe this');
  const dm = message({imageMessage: {}});
  dm.key.remoteJid = owner + '@s.whatsapp.net';
  assert.equal(extractMessage(dm, options).kind, 'image');
  assert.equal(extractMessage(dm, {...options, senderPhone: '15555550999'}), null);
});

test('three runtime modes, block precedence, pause and live whitelist changes', () => {
  const member='15555550200', stranger='15555550900';
  const policy={owner,mode:'whitelist',groups:['123@g.us'],members:[{phone:member,allowed:1,blocked:0}],group_trigger:'mention'};
  assert.equal(canReply(policy,member,'123@g.us',true),true);
  assert.equal(canReply(policy,stranger,'123@g.us',true),false);
  assert.equal(canReply({...policy,mode:'public'},stranger,'other@g.us',true),true);
  assert.equal(canReply({...policy,mode:'public',members:[{phone:stranger,blocked:1}]},stranger,'other@g.us',true),false);
  assert.equal(canReply({...policy,mode:'owner'},member,'123@g.us',true),false);
  assert.equal(canReply({...policy,mode:'owner',paused:true},owner,'other@g.us',true),true);
  assert.equal(canReply({...policy,paused:true},member,'123@g.us',true),false);
  assert.equal(extractMessage(message({conversation:'/admin status'}),{...options,policy}).text,'/admin status');
  assert.equal(extractMessage(message({conversation:'normal chat'}),{...options,policy}),null);
  assert.ok(extractMessage(message({conversation:'normal chat'}),{...options,policy:{...policy,group_trigger:'all'}}));
});

test('quiet groups only react to commands, mentions, known replies, or documents', () => {
  assert.equal(extractMessage(message({ conversation: 'ordinary chat' }), options), null);
  assert.ok(extractMessage(message({ conversation: '/assistant hello' }), options));
  assert.equal(extractMessage(message({ conversation: '/assistantXYZ hello' }), options), null);
  assert.ok(extractMessage(message({ extendedTextMessage: { text: 'Hi', contextInfo: { mentionedJid: [bot] } } }), options));
  assert.ok(extractMessage(message({ extendedTextMessage: { text: 'Hi', contextInfo: { stanzaId: 'our-message' } } }), options));
  assert.equal(extractMessage(message({ extendedTextMessage: { text: 'Hi', contextInfo: { stanzaId: 'fake', participant: bot } } }), options), null);
  assert.equal(extractMessage(message({ documentMessage: { fileName: 'manual.pdf' } }), options).kind, 'document');
});

test('unauthorized senders, other groups, history, and own messages are ignored', () => {
  const m = message({ conversation: '/assistant hello' });
  assert.equal(extractMessage(m, { ...options, senderPhone: '15555550999' }), null);
  assert.equal(extractMessage(m, { ...options, groupId: '456@g.us' }), null);
  assert.equal(extractMessage({ ...m, key: { ...m.key, fromMe: true } }, options), null);
  assert.equal(extractMessage({ ...m, messageTimestamp: 100 }, options), null);
});

test('DMs require no group trigger; LIDs need provider-supplied mapping', async () => {
  const m = message({ conversation: 'hello' });
  m.key.remoteJid = '99@lid';
  assert.equal(extractMessage(m, options).channel, 'dm');
  assert.equal(await resolvePhone('99@lid', null, mapping), owner);
  assert.equal(await resolvePhone('77@lid', null, mapping), null);
  assert.equal(await resolvePhone('77@lid', `${owner}@s.whatsapp.net`, mapping), owner);
  assert.equal(await resolvePhone(`${owner}:2@s.whatsapp.net`, null, mapping), owner);
});

test('other group participants do not block approved senders; bot must be present', () => {
  const metadata = { participants: [{ id: bot }, { id: '99@lid' }] };
  assert.equal(approvedGroup(metadata, options.botIds), true);
  metadata.participants.push({ id: '555@lid' });
  assert.equal(approvedGroup(metadata, options.botIds), true);
  assert.equal(approvedGroup({ participants: [{ id: '555@lid' }] }, options.botIds), false);
});

test('approved regular members can send group and DM messages and documents', () => {
  const memberOptions = { ...options, senderPhone: '15555550200' };
  assert.ok(extractMessage(message({ conversation: '/assistant hello' }), memberOptions));
  assert.ok(extractMessage(message({ documentMessage: { fileName: 'manual.pdf' } }), memberOptions));
  assert.equal(extractMessage(message({ conversation: 'ordinary discussion' }), memberOptions), null);
  const dm = message({ conversation: 'hello' });
  dm.key.remoteJid = '15555550200@s.whatsapp.net';
  assert.equal(extractMessage(dm, memberOptions).channel, 'dm');
  assert.equal(extractMessage(dm, { ...memberOptions, approvedPhones: new Set([owner]) }), null);
  assert.equal(extractMessage(message({ documentMessage: { fileName: 'manual.pdf' } }),
    { ...memberOptions, approvedPhones: new Set([owner]) }), null);
  assert.equal(extractMessage(dm, { ...memberOptions, approvedPhones: undefined }), null);
});
