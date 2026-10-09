import test from 'node:test';
import assert from 'node:assert/strict';
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
