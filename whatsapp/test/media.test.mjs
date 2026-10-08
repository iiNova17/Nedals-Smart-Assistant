import test from 'node:test';
import assert from 'node:assert/strict';
import { Readable } from 'node:stream';
import { mkdtemp, readFile, readdir, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { saveAttachment, discardAttachment, MAX_BYTES } from '../src/media.mjs';

test('streams an attachment and removes the temporary copy after processing', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'nedal-media-'));
  try {
    const result = await saveAttachment(dir, 'event', async () => Readable.from([Buffer.from('%PDF-'), Buffer.from('test')]));
    assert.match(result.file_token, /^[a-f0-9]{64}$/);
    assert.equal(await readFile(join(dir, result.file_token + '.bin'), 'utf8'), '%PDF-test');
    await discardAttachment(dir, result.file_token);
    assert.deepEqual(await readdir(dir), []);
  } finally { await rm(dir, { recursive: true, force: true }); }
});

test('enforces actual size even if declared size is missing or false', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'nedal-media-'));
  try {
    const result = await saveAttachment(dir, 'large', async () => Readable.from([Buffer.alloc(MAX_BYTES), Buffer.from('x')]));
    assert.equal(result.media_error, 'too_large');
    assert.deepEqual(await readdir(dir), []);
    const rejected = await saveAttachment(dir, 'declared-large', () => { throw new Error('must not download'); }, MAX_BYTES + 1);
    assert.equal(rejected.media_error, 'too_large');
  } finally { await rm(dir, { recursive: true, force: true }); }
});

test('download errors leave no partial file', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'nedal-media-'));
  try {
    const result = await saveAttachment(dir, 'failed', async () => { throw new Error('network'); });
    assert.equal(result.media_error, 'download_failed');
    assert.deepEqual(await readdir(dir), []);
  } finally { await rm(dir, { recursive: true, force: true }); }
});
