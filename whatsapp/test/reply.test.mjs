import test from 'node:test';
import assert from 'node:assert/strict';

test('group reminders carry real mention metadata; DMs do not', async () => {
  const calls = [];
  const socket = { sendMessage: async (...args) => { calls.push(args); return {key: {id: 'sent'}}; } };
  await sendReply(socket, '123@g.us', 'Reminder @15555550100', undefined,
    ['15555550100@s.whatsapp.net', 'bad', '15555550100@s.whatsapp.net']);
  assert.deepEqual(calls[0][1].mentions, ['15555550100@s.whatsapp.net']);
  await sendReply(socket, '15555550100@s.whatsapp.net', 'Private reminder', undefined,
    ['15555550100@s.whatsapp.net']);
  assert.equal(calls[1][1].mentions, undefined);
});
import { quoteFor, sendReply } from '../src/reply.mjs';
const message={key:{id:'incoming',remoteJid:'123@g.us',participant:'15555550100@s.whatsapp.net'},message:{documentMessage:{fileName:'Manual.pdf',mediaKey:Buffer.from('secret')}}};
test('quoted reply stores minimal content and links to the triggering message', async()=>{
 const quote=quoteFor(message);
 assert.equal(quote.message.conversation,'Manual.pdf');
 assert.equal(JSON.stringify(quote).includes('mediaKey'),false);
 let args;
 await sendReply({sendMessage:async(...a)=>{args=a;return {key:{id:'sent'}}}},'123@g.us','**Done**',message);
 assert.equal(args[1].text,'*Done*');
 assert.equal(args[2].quoted.key.id,'incoming');
});
test('ambiguous send failure is never retried as an unquoted duplicate', async()=>{
 let attempts=0;
 await assert.rejects(sendReply({sendMessage:async()=>{attempts++;throw Error('lost acknowledgement')}},'123@g.us','Done',message));
 assert.equal(attempts,1);
});
test('notifications and mismatched quote destinations are sent without quotes',async()=>{
 for (const quote of [undefined,message]) {
  let options;
  await sendReply({sendMessage:async(_chat,_body,o)=>{options=o}},'456@g.us','Reminder',quote);
  assert.deepEqual(options,{});
 }
});

test('extractReplyContext extracts text, audio, and sticker quoted metadata safely', async()=>{
 const { extractReplyContext } = await import('../src/reply.mjs');
 const textReply = {
  key: { remoteJid: '123@g.us', id: 'curr1' },
  message: {
   extendedTextMessage: {
    text: 'What time is this?',
    contextInfo: {
     stanzaId: 'orig1',
     participant: '15555550100@s.whatsapp.net',
     quotedMessage: { conversation: 'The arm review is Friday at 6' }
    }
   }
  }
 };
 const res = extractReplyContext(textReply);
 assert.equal(res.message_id, 'orig1');
 assert.equal(res.author_identifier, '15555550100');
 assert.equal(res.content_type, 'text');
 assert.equal(res.text_or_caption, 'The arm review is Friday at 6');

 const lidReply = {
  key: { remoteJid: '123@g.us', id: 'curr2' },
  message: {
   extendedTextMessage: {
    text: 'Check this',
    contextInfo: {
     stanzaId: 'orig2',
     participant: '99887766@lid',
     quotedMessage: { audioMessage: { ptt: true } }
    }
   }
  }
 };
 const resLid = extractReplyContext(lidReply);
 assert.equal(resLid.message_id, 'orig2');
 assert.equal(resLid.author_identifier, '99887766@lid');
 assert.equal(resLid.content_type, 'audio');
 assert.equal(resLid.has_attachment, true);
});
