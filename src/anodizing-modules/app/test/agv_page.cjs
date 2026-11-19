const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const scenario = process.argv[2];
const source = fs.readFileSync(path.join(__dirname, '../anodizing_web/web/agv.js'), 'utf8');
const storage = new Map();
const requests = [];
const radio = {device: '/dev/ttyUSB0', description: 'CH340'};
const other = {device: '/dev/ttyUSB1', description: 'Other adapter'};
const onboard = {device: '/dev/ttyS0', description: 'n/a'};
let storageReads = 0, storageWrites = 0;

async function load(ports) {
  const fields = new Map();
  function element(initialId) {
    const listeners = {};
    const field = {
      value: '', checked: false, dataset: {}, classList: {toggle() {}},
      addEventListener(type, fn) { listeners[type] = fn; },
      emit(type) { return listeners[type]({currentTarget: this}); },
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
  let sequence = 0;
  vm.runInNewContext(source, {
    document: {getElementById: field, querySelector: () => ({content: 'fixture'}), createElement: () => element()},
    window: {addEventListener() {}},
    Option: class { constructor(text, value) { Object.assign(this, {text, value}); } },
    performance: {now: () => 0}, setInterval() {},
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
      assert.equal(route, '/api/agv');
      const body = options.body && JSON.parse(options.body);
      if (body) requests.push(body.action);
      const response = {connection_id: 1, sequence: ++sequence, connected: false, armed: false, log: [], results: {}};
      if (!body) Object.assign(response, {serial_available: true, ports, commands: [],
        settings: {mode: 'simulator', address: '00 99', channel: '08', port: null}});
      else if (scenario === 'rejected') return {ok: false, json: async () => ({error: 'Fixture rejection'})};
      else Object.assign(response, {connected: true, connection_id: 2,
        settings: {mode: body.mode, port: body.mode === 'serial' ? body.port : null,
          address: body.address.toUpperCase(), channel: body.channel.toUpperCase()}});
      return {ok: true, json: async () => response};
    },
  }, {filename: 'agv.js'});
  await new Promise(setImmediate);
  return get;
}

function selectSerial(get) {
  get('mode').value = 'serial';
  get('mode').emit('input');
}

async function run() {
  const get = await load(scenario === 'reload' || scenario === 'ambiguous' ? [onboard, radio, other] : [onboard, radio]);
  if (scenario === 'reload') {
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
    assert.deepEqual(requests, ['connect']);

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
  } else {
    assert.equal(get('port').value, '');
    selectSerial(get);
    assert.equal(get('port').value, scenario === 'single-port' ? radio.device : '');
    assert.deepEqual(requests, []);
  }
}

run().catch(error => { console.error(error); process.exitCode = 1; });
