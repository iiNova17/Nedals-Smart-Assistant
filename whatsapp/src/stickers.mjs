// Fetch only authenticated outbox media, never model-supplied file paths or URLs.
export async function fetchSticker(backend, token, notificationId, fetcher = fetch) {
  const response = await fetcher(new URL(
    '/internal/whatsapp/notifications/' + encodeURIComponent(notificationId) + '/media', backend,
  ), { headers: { Authorization: `Bearer ${token}` }, signal: AbortSignal.timeout(10000) });
  if (!response.ok || response.headers.get('content-type')?.split(';')[0] !== 'image/webp') {
    await response.body?.cancel();
    throw new Error('Sticker media unavailable');
  }
  const chunks = [];
  let size = 0;
  for await (const chunk of response.body) {
    size += chunk.length;
    if (size > 1024 * 1024) throw new Error('Sticker size limit exceeded');
    chunks.push(chunk);
  }
  const data = Buffer.concat(chunks);
  if (data.length < 20 || data.toString('ascii', 0, 4) !== 'RIFF' ||
      data.toString('ascii', 8, 12) !== 'WEBP' || data.readUInt32LE(4) + 8 !== data.length) {
    throw new Error('Invalid sticker media');
  }
  return data;
}

export async function sendSticker(socket, chat, data) {
  // A failed send may already have reached WhatsApp. The caller marks it uncertain;
  // never retry here or replace it with a plain-text placeholder.
  return socket.sendMessage(chat, { sticker: data });
}
