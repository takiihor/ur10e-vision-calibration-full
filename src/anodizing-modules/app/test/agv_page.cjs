const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const scenario = process.argv[2];
const web = path.join(__dirname, '../anodizing_web/web');
const source = fs.readFileSync(path.join(web, 'agv-record.mjs'), 'utf8').split(/^export /m)[0] +
  fs.readFileSync(path.join(web, 'agv.js'), 'utf8').replace(/^import .*?;\n/s, '');
const storage = new Map();
const requests = [];
const radio = {device: '/dev/ttyUSB0', description: 'CH340'};
const other = {device: '/dev/ttyUSB1', description: 'Other adapter'};
const onboard = {device: '/dev/ttyS0', description: 'n/a'};
let storageReads = 0, storageWrites = 0;

async function load(ports) {
  const fields = new Map();
  const window = element();
  let stopReads = 0;
  function element(initialId) {
    const listeners = {};
    const field = {
      value: '', checked: false, dataset: {}, classList: {toggle() {}},
      addEventListener(type, fn) { listeners[type] = fn; },
      emit(type, event = {}) {
        if (listeners[type]) return listeners[type]({currentTarget: this, ...event});
        if (this !== window) return window.emit(type, {target: this, ...event});
      },
      append() {}, setAttribute() {}, removeAttribute() {},
      replaceChildren(...options) { this.options = options; if (!options.some(option => option.value === this.value)) this.value = ''; },
      set id(id) { this.elementId = id; fields.set(id, this); },
      get id() { return this.elementId; },
    };
    if (initialId) field.id = initialId;
    return field;
  }
  function field(id) {
    return fields.get(id) ?? element(id);
  }
  const get = id => field('agv-' + id);
  get('mode').value = 'simulator';
  get('address').value = '00 01';
  get('channel').value = '04';
  get('stations-config').value = '3';
  let sequence = 0, now = 0, tick, stopSent;
  vm.runInNewContext(source, {
    document: {getElementById: field, querySelector: () => ({content: 'fixture'}), createElement: () => element()},
    window,
    Option: class { constructor(text, value) { Object.assign(this, {text, value}); } },
    performance: {now: () => now}, setInterval(callback) { tick = callback; },
    localStorage: {
      getItem(key) {
        storageReads++;
        if (scenario === 'blocked') throw new Error('Storage is blocked');
        return storage.get(key) ?? null;
      },
      setItem(key, value) {
        storageWrites++;
        if (scenario === 'blocked') throw new Error('Storage is blocked');
        storage.set(key, value);
      },
    },
    fetch: async (route, options) => {
      assert.match(route, /^\/api\/agv(?:\?(?:cached|ports)=1)?$/);
      const body = options.body && JSON.parse(options.body);
      if (body) requests.push(body);
      const response = {connection_id: 1, sequence: ++sequence, connected: false, armed: false, log: [], results: {}};
      if (!body) {
        Object.assign(response, {serial_available: true, ports, commands: [],
          settings: {mode: 'simulator', address: '00 99', channel: '08', port: null}});
        if (scenario === 'motion-approval') Object.assign(response, {
          connected: true, armed: true, stop_generation: 7, status_hex: 'station report', status_age_s: .2,
          status: {mode: 2, current_station: 1, max_stations: 3, stopped: true},
          commands: [{name: 'forward', label: 'Forward', motion: true},
            {name: 'rotate_clockwise', label: 'Rotate', motion: true}],
        });
        if (['station-range', 'station-unlearned'].includes(scenario)) Object.assign(response, {
          connected: true, armed: true, stop_generation: 7, status_hex: 'station report', status_age_s: .2,
          status: {mode: 2, current_station: 2, max_stations: scenario === 'station-range' ? 1 : 0, stopped: true},
          commands: [{name: 'call_station', label: 'Call station', motion: true,
            argument: {label: 'Station number', min: 1, max: 255, default: 1}}],
        });
        if (scenario === 'standard') Object.assign(response, {
          connected: true, status_hex: 'standard report', status_age_s: .2,
          status: {mode: 0, current_station: 7, max_stations: 1, position_units: 20, stopped: true},
          commands: [{name: 'forward', label: 'Forward', motion: true}, {name: 'reverse', label: 'Reverse', motion: true}],
          link_quality: {sent: 4, acknowledged: 1, resent: 2, no_reply: 1, timed_report_interval_s: 1.2},
        });
        if (stopSent) {
          const late = ++stopReads > 1;
          Object.assign(response, {...stopSent, sequence,
            results: {stop: {...stopSent.results.stop, tries: 5,
              ...(late ? {state: 'acknowledged late', ack_elapsed_s: 1.102} : {state: 'no reply'})}},
            link_quality: {sent: 5, acknowledged: +late, resent: 4, no_reply: +!late, timed_report_interval_s: 1.2}});
        }
      } else if (scenario === 'rejected') return {ok: false, json: async () => ({error: 'Fixture rejection'})};
      else if (scenario === 'motion-approval' && body.command === 'forward') Object.assign(response, {
        connected: true, armed: true, stop_generation: 7, settings: {mode: 'simulator'},
        results: {forward: {tx_id: sequence, state: 'sent', sent_at: '2026-11-19T12:34:56.789Z'}},
      });
      else if (scenario === 'motion-approval' && body.command === 'rotate_clockwise') Object.assign(response, {
        connected: true, armed: true, stop_generation: 7, settings: {mode: 'simulator'},
        results: {rotate_clockwise: {tx_id: sequence, state: 'no reply', tries: 1,
          sent_at: '2026-11-19T12:34:56.789Z', warning: 'Delivery is uncertain. Check the AGV before trying again.'}},
      });
      else if (scenario === 'motion-approval' && body.command === 'stop') Object.assign(response, {
        connected: true, armed: true, stop_generation: 8, urgent_command: 'pause', settings: {mode: 'simulator'},
        results: {pause: {tx_id: sequence, state: 'sent', sent_at: '2026-11-19T12:34:56.789Z'}},
      });
      else if (body.action === 'send') stopSent = Object.assign(response, {connected: true, connection_id: 2,
        settings: {mode: 'simulator', address: '00 01', channel: '04', port: null},
        results: {stop: {tx_id: sequence, state: 'sent', sent_at: '2026-11-19T12:34:56.789Z'}}});
      else Object.assign(response, {connected: true, connection_id: 2,
        settings: {mode: body.mode, port: body.mode === 'serial' ? body.port : null,
          address: body.address.toUpperCase(), channel: body.channel.toUpperCase()}});
      return {ok: true, json: async () => response};
    },
  }, {filename: 'agv.js'});
  await new Promise(setImmediate);
  get.refresh = async () => { now += 6000; await tick(); };
  return get;
}

function selectSerial(get) {
  get('mode').value = 'serial';
  get('mode').emit('input');
}

async function run() {
  const get = await load(scenario === 'reload' || scenario === 'ambiguous' ? [onboard, radio, other] : [onboard, radio]);
  if (scenario === 'standard') {
    assert.equal(get('forward').textContent, 'Forward');
    assert.equal(get('reverse').textContent, 'Reverse');
    assert.match(get('station-note').textContent, /station-call function/);
    assert.match(get('station-label').textContent, /Stop counter/);
    assert.match(get('link-quality').textContent, /Sent 4.*Acknowledged 1.*Resent 2.*No reply 1.*1.2 s/);
  }
  if (scenario === 'station-range' || scenario === 'station-unlearned') {
    get('call_station-value').valueAsNumber = 2;
    get('call_station-value').emit('input');
    if (scenario === 'station-range') {
      assert.equal(get('stations').options.length, 2);
      assert.equal(get('station-1').textContent, 'Go to station 1');
      assert.equal(get('station-2').textContent, 'Go to station 2 · Current');
      assert.equal(get('call_station').disabled, false);
      get('call_station-value').valueAsNumber = 3;
      get('call_station-value').emit('input');
      assert.equal(get('call_station').disabled, true);
      assert.match(get('call_station-why').textContent, /from 1 to 2/);
      await get('station-2').emit('click');
      assert.equal(requests.at(-1).value, 2);
    } else {
      assert.equal(get('stations').options.length, 0);
      assert.equal(get('call_station').disabled, true);
      assert.match(get('station-label').textContent, /Stop counter/);
      assert.deepEqual(requests, []);
    }
  } else if (scenario === 'reload') {
    selectSerial(get);
    get('port').value = radio.device;
    get('address').value = 'aa 12';
    get('channel').value = '0b';
    await get('connect').emit('click');

    assert.equal(storage.size, 1);
    const [[key, value]] = storage;
    assert.deepEqual(JSON.parse(value), {mode: 'serial', port: radio, address: 'AA 12', channel: '0B'});
    storage.set(key, JSON.stringify({...JSON.parse(value), armed: true, auto: true}));
    const restored = await load([onboard, other, radio]);
    assert.deepEqual(['mode', 'port', 'address', 'channel'].map(id => restored(id).value),
      ['serial', radio.device, 'AA 12', '0B']);
    assert.equal(restored('armed').checked, false);
    assert.equal(restored('auto').checked, false);
    assert.equal(restored('link').textContent, 'Disconnected');
    assert.deepEqual(requests.map(body => body.action), ['connect']);

    const fallback = await load([onboard, other]);
    assert.equal(fallback('port').value, other.device);
  } else if (scenario === 'blocked') {
    await get('connect').emit('click');
    assert.equal(storageReads, 3);
    assert.equal(storageWrites, 1);
    assert.equal(get('disconnect').disabled, false);
    assert.match(get('outcome').textContent, /Connected · Simulator/);
  } else if (scenario === 'rejected') {
    await get('connect').emit('click');
    assert.equal(storageWrites, 0);
    assert.equal(get('disconnect').disabled, true);
  } else if (scenario === 'motion-approval') {
    await get('rotate_clockwise').emit('click');
    assert.match(get('rotate_clockwise-outcome').textContent, /Delivery is uncertain/);
    await get.refresh();
    await get('forward').emit('click');
    assert.equal(requests.at(-1).stop_generation, 7);
    await get('stop').emit('click');
    assert.match(get('stop-outcome').textContent, /Pause sent/);
  } else if (scenario === 'escape-stop') {
    const press = (key, repeat = false) => get('mark-text').emit('keydown', {key, repeat, preventDefault() {}});
    await press('Escape');
    assert.deepEqual(requests, []);
    assert.equal(get('outcome').textContent, 'Connect first.');

    await get('connect').emit('click');
    assert.equal(get('armed').checked, false);
    await press('s');
    await press('Escape', true);
    assert.equal(requests.length, 1);

    const stop = press('Escape');
    assert.equal(get('outcome').textContent, 'Sending…');
    await stop;
    assert.match(get('outcome').textContent, /^Stop sent \d\d:\d\d:\d\d\.789 \(Esc\)/);
    assert.equal(get('stop-outcome').textContent, get('outcome').textContent);

    await get('stop').emit('click');
    const stopRequest = {action: 'send', command: 'stop', connection_id: 2, value: null, background: false};
    assert.deepEqual(requests.slice(1), [stopRequest, stopRequest]);
    assert.match(get('outcome').textContent, /^Stop sent \d\d:\d\d:\d\d\.789 · /);
    await get.refresh();
    assert.equal(get('stop-outcome').textContent, 'No reply from the AGV after 5 tries. Check the radio, then try again.');
    assert.equal(get('outcome').textContent, get('stop-outcome').textContent);
    await get.refresh();
    assert.match(get('stop-outcome').textContent, /AGV acknowledged late/);
    assert.doesNotMatch(get('stop-outcome').textContent, /No reply|waiting/);
    assert.equal(get('outcome').textContent, get('stop-outcome').textContent);
  } else {
    assert.equal(get('port').value, '');
    selectSerial(get);
    assert.equal(get('port').value, scenario === 'single-port' ? radio.device : '');
    assert.deepEqual(requests, []);
  }
}

async function pickPlaceUrgent() {
  const source = fs.readFileSync(path.join(__dirname, '../anodizing_web/web/manual-pick-place.js'), 'utf8');
  const fields = new Map();
  const field = id => {
    if (!fields.has(id)) fields.set(id, {id, value: '', valueAsNumber: NaN, checked: false, dataset: {},
      checkValidity: () => true, addEventListener(type, fn) { this[type] = fn; }, setAttribute() {}});
    return fields.get(id);
  };
  const keyboard = {};
  const timers = [];
  const sent = {state: 'sent', tx_id: 1, sent_at: '2026-11-19T12:34:56.789Z', tries: 1};
  let reply = null;
  vm.runInNewContext(source, {
    document: {getElementById: field, querySelector: () => ({content: 'fixture'}),
      addEventListener(type, fn) { keyboard[type] = fn; }},
    window: {}, localStorage: {getItem: () => null},
    setTimeout: callback => timers.push(callback),
    fetch: async (route, options) => {
      if (route === '/api/agv-travel-pose') return new Promise(() => {});
      if (options.body) requests.push(JSON.parse(options.body));
      const body = options.body && JSON.parse(options.body);
      return {ok: true, json: async () => {
        if (route === '/api/agv-link') return {connected: true, results: reply ? {pause: reply} : {}};
        if (route === '/api/agv' && body?.command === 'stop')
          return {connected: true, recipe_active: true, urgent_command: 'pause', results: {pause: sent}};
        return {};
      }};
    },
  }, {filename: 'manual-pick-place.js'});
  await new Promise(setImmediate);
  keyboard.keydown({key: 'Escape', repeat: false, preventDefault() {}});
  assert.equal(field('pick-agv-outcome').textContent, 'Sending…');
  assert.equal(field('pick-agv-stop').disabled, false);
  await new Promise(setImmediate);
  assert.deepEqual(requests, [{action: 'send', command: 'stop'}]);
  assert.match(field('pick-agv-outcome').textContent, /Pause sent.*\(Esc\)/);
  reply = {...sent, state: 'confirmed by status', confirmed_stopped: false};
  await timers.shift()();
  assert.match(field('pick-agv-outcome').textContent, /pause active/i);
  assert.doesNotMatch(field('pick-agv-outcome').textContent, /reports stopped/);
  reply = {...sent, state: 'no reply', tries: 4};
  await timers.shift()();
  assert.match(field('pick-agv-outcome').textContent, /No reply.*4 tries/);
  assert.equal(field('pick-agv-outcome').textContent, field('pick-agv-stop-outcome').textContent);
}

(scenario === 'pick-place-urgent' ? pickPlaceUrgent() : run()).catch(error => {
  console.error(error);
  process.exitCode = 1;
});
