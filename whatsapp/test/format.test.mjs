import test from 'node:test';
import assert from 'node:assert/strict';
import { formatWhatsApp } from '../src/format.mjs';
test('WhatsApp headings, bold, bullets and source links are readable', () => {
 assert.equal(formatWhatsApp('## **Motor**\n\n* **Torque:** 12 Nm\n\n[Manual](https://example.com/manual.pdf)'), '*Motor*\n\n- *Torque:* 12 Nm\n\nManual\nhttps://example.com/manual.pdf');
});
test('preserves code, units, citations and already native bold', () => {
 const text='*Result* ✅\n12 Nm [1]\n`a ** b`\n```python\n# heading\nx ** 2\n```';
 assert.equal(formatWhatsApp(text),text);
 assert.equal(formatWhatsApp(formatWhatsApp('**Result**')), '*Result*');
});
