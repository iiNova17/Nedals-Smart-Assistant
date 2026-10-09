import test from 'node:test';
import assert from 'node:assert/strict';
import { fetchSticker, sendSticker } from '../src/stickers.mjs';

const data = Buffer.from('524946461000000057454250565038200400000074657374', 'hex');

test('sticker outbox fetch uses authenticated endpoint and sends actual media bytes', async () => {
  const bytes = await fetchSticker('http://127.0.0.1:8000', 'test-token', 'delivery-id', async (url, options) => {
    assert.equal(url.pathname, '/internal/whatsapp/notifications/delivery-id/media');
    assert.equal(options.headers.Authorization, 'Bearer test-token');
    return new Response(data, { headers: { 'content-type': 'image/webp' } });
  });
  let calls = 0;
  const socket = { sendMessage: async (chat, message) => {
    calls++;
    assert.equal(chat, '123@g.us');
    assert.deepEqual(message, { sticker: data });
    return { key: { id: 'sent-sticker' } };
  } };
  assert.equal((await sendSticker(socket, '123@g.us', bytes)).key.id, 'sent-sticker');
  assert.equal(calls, 1);
});

test('sticker media rejects unauthorized, invalid and oversized responses', async () => {
  for (const response of [
    new Response('', { status: 403 }),
    new Response('not webp', { headers: { 'content-type': 'image/webp' } }),
    new Response(Buffer.alloc(1024 * 1024 + 1), { headers: { 'content-type': 'image/webp' } }),
  ]) {
    await assert.rejects(fetchSticker('http://localhost:8000', 'token', 'id', async () => response));
  }
});

test('uncertain sticker send is not retried or replaced by text', async () => {
  let calls = 0;
  await assert.rejects(sendSticker({ sendMessage: async () => {
    calls++;
    throw new Error('Connection lost after upload');
  } }, '123@g.us', data));
  assert.equal(calls, 1);
});
