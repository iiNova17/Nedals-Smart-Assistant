import { createHash } from 'node:crypto';
import { mkdir, open, readdir, stat, unlink, rename } from 'node:fs/promises';
import { join } from 'node:path';

export const MAX_BYTES = 25 * 1024 * 1024;

export async function saveAttachment(directory, eventId, getStream, declaredSize = 0) {
  if (Number(declaredSize) > MAX_BYTES) return { media_error: 'too_large' };
  await mkdir(directory, { recursive: true });
  let occupied = 0;
  for (const name of await readdir(directory)) {
    if (!/^[a-f0-9]{64}\.(bin|part)$/.test(name)) continue;
    const path = join(directory, name);
    const info = await stat(path);
    if (Date.now() - info.mtimeMs > 86400000) await unlink(path);
    else occupied += info.size;
  }
  if (occupied + MAX_BYTES > 250 * 1024 * 1024) return { media_error: 'spool_full' };
  const token = createHash('sha256').update(eventId).digest('hex');
  const partial = join(directory, token + '.part');
  const destination = join(directory, token + '.bin');
  const file = await open(partial, 'w', 0o600);
  let stream;
  let timer;
  let size = 0;
  let failure = '';
  try {
    stream = await getStream();
    timer = setTimeout(() => stream.destroy(new Error('Download timeout')), 45000);
    for await (const chunk of stream) {
      size += chunk.length;
      if (size > MAX_BYTES) { failure = 'too_large'; break; }
      await file.writeFile(chunk);
    }
    if (!size && !failure) failure = 'download_failed';
  } catch { failure ||= 'download_failed'; }
  finally { clearTimeout(timer); stream?.destroy(); await file.close(); }
  if (failure) {
    await unlink(partial).catch(() => {});
    return { media_error: failure };
  }
  await rename(partial, destination);
  return { file_token: token };
}

export async function discardAttachment(directory, token) {
  if (/^[a-f0-9]{64}$/.test(token ?? '')) {
    await unlink(join(directory, token + '.bin')).catch(() => {});
  }
}
