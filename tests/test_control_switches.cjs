const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { createClient } = require('../frontend/control-switches.js');

function fixture({ status = 200, body = {}, confirmed = false, omitControl = false } = {}) {
  let clock = 1000000, posts = 0;
  const client = createClient({
    now: () => clock,
    attempts: 2,
    wait: async () => { clock += 1000; },
    fetch: async (url, init) => {
      if (init.method === 'POST') {
        posts++;
        return { ok: status >= 200 && status < 300, status, json: async () => body };
      }
      const control = { room_disabled: posts && confirmed ? true : false,
        device_soft_off: posts && confirmed ? true : false, observed_at: clock / 1000 };
      return { ok: true, json: async () => ({ devices: { br_ac: omitControl ? {} : { control_switches: control } } }) };
    }
  });
  return { client, posts: () => posts };
}

for (const flag of ['room_disabled', 'device_soft_off']) {
  test('HTTP 501 cannot falsely close ' + flag, async () => {
    const { client, posts } = fixture({ status: 501, body: { demo: true, error: 'preview rejects writes' } });
    await client.refresh(true);
    assert.equal(client.value('br_ac', flag), false);
    const operation = client.change('br_ac', flag, true, '/api/toggle/test');
    assert.equal(client.isPending('br_ac', flag), true);
    await assert.rejects(operation, /HTTP 501/);
    assert.equal(client.value('br_ac', flag), false);
    assert.equal(client.isPending('br_ac', flag), false);
    assert.equal(posts(), 1);
  });
}

test('HTTP 200 with service error is rejected', async () => {
  const { client } = fixture({ body: { success: false } });
  await assert.rejects(client.change('br_ac', 'room_disabled', true, '/api/toggle/test'), /服务未确认/);
  assert.equal(client.value('br_ac', 'room_disabled'), false);
});

test('API success is insufficient when controller still reads enabled', async () => {
  const { client } = fixture({ body: { br: true } });
  await assert.rejects(client.change('br_ac', 'room_disabled', true, '/api/toggle/test'), /温控程序尚未确认/);
  assert.equal(client.value('br_ac', 'room_disabled'), false);
});

test('new controller observation confirms change and repeated clicks are deduplicated', async () => {
  const { client, posts } = fixture({ confirmed: true });
  const first = client.change('br_ac', 'room_disabled', true, '/api/toggle/test');
  const second = client.change('br_ac', 'room_disabled', true, '/api/toggle/test');
  assert.equal(first, second);
  assert.equal(await first, true);
  assert.equal(client.value('br_ac', 'room_disabled'), true);
  assert.equal(client.isPending('br_ac', 'room_disabled'), false);
  assert.equal(posts(), 1);
});

test('legacy snapshots cannot be mistaken for controller confirmation', async () => {
  const { client } = fixture({ omitControl: true });
  await client.refresh(true);
  assert.equal(client.value('br_ac', 'room_disabled'), null);
  await assert.rejects(client.change('br_ac', 'room_disabled', true, '/api/toggle/test'), /温控程序尚未确认/);
});

test('network failure does not preserve a misleading known state', async () => {
  let fail = false;
  const client = createClient({ now: () => 1000000, fetch: async () => {
    if (fail) throw new Error('network offline');
    return { ok: true, json: async () => ({ devices: { br_ac: {
      control_switches: { room_disabled: true, observed_at: 1000 }
    } } }) };
  } });
  await client.refresh(true);
  assert.equal(client.value('br_ac', 'room_disabled'), true);
  fail = true;
  await assert.rejects(client.refresh(true), /network offline/);
  assert.equal(client.value('br_ac', 'room_disabled'), null);
});

test('stale, null and string flags remain unknown', async () => {
  for (const [flag, observed] of [[true, 1], [null, 1000], ['false', 1000]]) {
    const client = createClient({ now: () => 1000000, fetch: async () => ({
      ok: true, json: async () => ({ devices: { br_ac: { control_switches: {
        room_disabled: flag, observed_at: observed
      } } } })
    }) });
    await client.refresh(true);
    assert.equal(client.value('br_ac', 'room_disabled'), null);
  }
});

test('all inline dashboard scripts remain valid JavaScript', () => {
  const html = fs.readFileSync(path.join(__dirname, '../frontend/state-machine-dashboard.html'), 'utf8');
  let count = 0;
  for (const match of html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi)) {
    if (/\bsrc\s*=/.test(match[1])) continue;
    new vm.Script(match[2], { filename: 'dashboard-inline-' + (++count) });
  }
  assert.ok(count > 0);
});
