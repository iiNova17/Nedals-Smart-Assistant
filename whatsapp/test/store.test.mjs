import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Store } from '../src/store.mjs';

test('auth buffers, key deletion, job dedup and uncertain sends survive restarts', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'nedal-wa-test-'));
  const path = join(dir, 'state.sqlite3');
  let store = new Store(path);
  try {
    const auth = store.authState();
    auth.saveCreds();
    await auth.state.keys.set({ session: { key1: Buffer.from([1, 2, 3]) } });
    const identity = auth.state.creds.signedIdentityKey.private;
    assert.equal(store.enqueue('event', 'chat', { text: 'test' }), true);
    assert.equal(store.enqueue('event', 'chat', { text: 'test' }), false);
    store.ready('event', 'reply');
    store.state('event', 'sending', 'outbound');
    store.close();
    store = new Store(path);
    const restored = store.authState();
    assert.deepEqual(restored.state.creds.signedIdentityKey.private, identity);
    assert.deepEqual((await restored.state.keys.get('session', ['key1'])).key1, Buffer.from([1, 2, 3]));
    assert.equal(store.next(), undefined); // Ambiguous sends never resend automatically.
    assert.equal(store.isOurReply('chat', 'outbound'), true);
    await restored.state.keys.set({ session: { key1: null } });
    assert.equal((await restored.state.keys.get('session', ['key1'])).key1, null);
  } finally { store.close(); rmSync(dir, { recursive: true, force: true }); }
});
