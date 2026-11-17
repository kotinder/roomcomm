const { test } = require('node:test');
const assert = require('node:assert/strict');
const ui = require('../static/message-json.js');

test('formatting preserves numeric tokens, duplicate keys, escapes and HTML as text', () => {
  const raw = '{"type":"service_offer","id":9007199254740993,"tiny":1e-400,"huge":1e400,"n":-0,"x":1,"x":2,"s":"</code><script>alert(1)</script> \\" \\\\ \\n","nested":[{},[],true,false,null,"Привет"]}';
  assert.ok(ui.inspect(raw));
  const formatted = ui.tokens(raw).map(t => t.text).join('');
  assert.deepEqual(JSON.parse(formatted), JSON.parse(raw));
  for (const value of ['9007199254740993', '1e-400', '1e400', '-0', '"x": 1', '"x": 2']) {
    assert.ok(formatted.includes(value), value);
  }
  assert.ok(formatted.includes('\n  "type": "service_offer"'));
  assert.equal(ui.inspect(raw).label, 'service_offer');
});

test('mixed prose and several JSON fences keep their order and surrounding text', () => {
  const raw = 'Вариант 1:\n```json\n{"type":"service_offer","price":8500}\n```\nЕщё:\n```JSON\n[1,2]\n```\nКонец';
  const parts = ui.split(raw);
  assert.deepEqual(parts, [
    {text:'Вариант 1:\n'}, {json:'{"type":"service_offer","price":8500}'},
    {text:'\nЕщё:\n'}, {json:'[1,2]'}, {text:'\nКонец'}
  ]);
});

test('raw objects, arrays, unlabelled fences, tilde fences and CRLF work', () => {
  for (const raw of ['{}', ' [1,2] ', '```\n{}\n```', '~~~json\r\n{}\r\n~~~']) {
    assert.equal(ui.split(raw).filter(p => p.json !== undefined).length, 1, raw);
  }
});

test('invalid JSON, incomplete fences, other languages and ordinary text remain literal', () => {
  for (const raw of ['hello {world}', '```json\n{"a":}\n```', '```json\n{}',
    '```python\n{}\n```', '```\n```json\n{}\n```\n```', '42', 'null', '"text"']) {
    assert.deepEqual(ui.split(raw), [{text:raw}], raw);
  }
});

test('valid JSON after an invalid fenced block is still recognized', () => {
  const raw = '```json\n{oops}\n```\n```json\n{}\n```';
  assert.deepEqual(ui.split(raw), [{text:'```json\n{oops}\n```\n'}, {json:'{}'}]);
});
