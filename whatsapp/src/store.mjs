import { DatabaseSync } from 'node:sqlite';
import { BufferJSON, initAuthCreds, proto } from '@whiskeysockets/baileys';

const encode = value => JSON.stringify(value, BufferJSON.replacer);
const decode = value => JSON.parse(value, BufferJSON.reviver);

export class Store {
  constructor(path) {
    this.db = new DatabaseSync(path);
    this.db.exec(`
      PRAGMA journal_mode=WAL;
      PRAGMA busy_timeout=5000;
      CREATE TABLE IF NOT EXISTS auth (key TEXT PRIMARY KEY, value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY, chat TEXT NOT NULL, payload TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'queued', reply TEXT, outbound_id TEXT,
        created INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt INTEGER NOT NULL DEFAULT 0
      );
      CREATE INDEX IF NOT EXISTS jobs_pending ON jobs(state, created);
      UPDATE jobs SET state='uncertain' WHERE state='sending';
    `);
  }
  read(key) {
    const row = this.db.prepare('SELECT value FROM auth WHERE key=?').get(key);
    return row ? decode(row.value) : null;
  }
  write(key, value) {
    if (value == null) this.db.prepare('DELETE FROM auth WHERE key=?').run(key);
    else this.db.prepare('INSERT OR REPLACE INTO auth VALUES (?,?)').run(key, encode(value));
  }
  authState() {
    const creds = this.read('creds') ?? initAuthCreds();
    return {
      state: {
        creds,
        keys: {
          get: async (type, ids) => Object.fromEntries(ids.map(id => {
            let value = this.read(`${type}:${id}`);
            if (type === 'app-state-sync-key' && value) {
              value = proto.Message.AppStateSyncKeyData.fromObject(value);
            }
            return [id, value];
          })),
          set: async data => {
            this.db.exec('BEGIN IMMEDIATE');
            try {
              for (const [type, values] of Object.entries(data)) {
                for (const [id, value] of Object.entries(values)) this.write(`${type}:${id}`, value);
              }
              this.db.exec('COMMIT');
            } catch (error) { this.db.exec('ROLLBACK'); throw error; }
          },
        },
      },
      saveCreds: () => this.write('creds', creds),
    };
  }
  enqueue(id, chat, payload) {
    const count = this.db.prepare("SELECT COUNT(*) n FROM jobs WHERE state IN ('queued','ready')").get().n;
    if (count >= 100) return false;
    return this.db.prepare('INSERT OR IGNORE INTO jobs(id,chat,payload,created) VALUES (?,?,?,?)')
      .run(id, chat, JSON.stringify(payload), Date.now()).changes > 0;
  }
  hasJob(id) { return Boolean(this.db.prepare('SELECT 1 FROM jobs WHERE id=?').get(id)); }
  next() {
    return this.db.prepare("SELECT * FROM jobs WHERE state IN ('queued','ready') AND next_attempt<=? ORDER BY created LIMIT 1")
      .get(Date.now());
  }
  ready(id, text) { this.db.prepare("UPDATE jobs SET state='ready',reply=? WHERE id=?").run(text, id); }
  state(id, state, outbound = null) {
    this.db.prepare('UPDATE jobs SET state=?,outbound_id=COALESCE(?,outbound_id) WHERE id=?')
      .run(state, outbound, id);
  }
  retry(job) {
    if (job.attempts >= 2) { this.ready(job.id, 'The assistant is temporarily unavailable. Please try again later.'); return; }
    this.db.prepare('UPDATE jobs SET attempts=attempts+1,next_attempt=? WHERE id=?')
      .run(Date.now() + 5000 * 2 ** job.attempts, job.id);
  }
  isOurReply(chat, id) {
    return Boolean(id && this.db.prepare("SELECT 1 FROM jobs WHERE chat=? AND outbound_id=? AND state IN ('sent','sending','uncertain')")
      .get(chat, id));
  }
  cleanup() {
    this.db.prepare("DELETE FROM jobs WHERE created<? AND state NOT IN ('queued','ready','sending')")
      .run(Date.now() - 7 * 86400000);
  }
  close() { this.db.close(); }
}
