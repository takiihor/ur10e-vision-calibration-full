const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const [web, moduleName, scenario] = process.argv.slice(2);
const read = name => fs.readFileSync(path.join(web, name), 'utf8');
const fields = new Map();
function field(id) {
  if (!fields.has(id)) {
    const events = {};
    fields.set(id, {
      id, value: '', defaultValue: '', checked: false, textContent: '', content: 'fixture', dataset: {},
      addEventListener(type, fn) { events[type] = fn; },
      emit(type, target = this) { return events[type]?.({target, stopPropagation() {}}); },
      querySelector: selector => selector === '.operation-body' ? field(selector) : null,
      querySelectorAll: () => [], hasAttribute: () => false,
      classList: {toggle() {}}, setAttribute() {}, append() {}, prepend() {}, click() {}
    });
  }
  return fields.get(id);
}

const html = read(moduleName + '.html');
for (const [tag, id] of html.matchAll(/<input\b[^>]*\bid="([^"]+)"[^>]*>/g))
  field(id).value = field(id).defaultValue = /\bvalue="([^"]*)"/.exec(tag)?.[1] ?? '';
const buttons = Array.from(html.matchAll(/data-action="([^"]+)"/g), ([, action]) => {
  const button = field('button-' + action);
  button.dataset.action = action;
  return button;
});
let documentInput;
const document = {
  body: {dataset: {module: moduleName}}, getElementById: field, querySelector: field,
  querySelectorAll: selector => selector === '[data-action]' ? buttons : [], createElement: field,
  addEventListener(type, fn) { if (type === 'input') documentInput = fn; }, dispatchEvent() {}
};

const storage = new Map();
const calibration = {path: 'calibrations/example/calibration.json', tcp: [0, 0, .125, 0, 0, 0]};
const defaults = {picking_boards: [], manifests: [], calibrations: scenario === 'listed' ? [calibration] : [],
  settings: {project: 'teaching/example', camera_provider: 'realsense', calibration: '', robot_ip: '',
    tcp: Array(6).fill(0), motion_model: 'planar', approach_mm: 100, target: {kind: 'charuco'}}};
if (moduleName === 'teaching' && scenario !== 'edit') {
  const handoff = {calibration: calibration.path, camera_provider: 'realsense'};
  if (scenario === 'unlisted') handoff.tcp = calibration.tcp;
  storage.set('ano-handoff-teaching', JSON.stringify(handoff));
}
if (moduleName === 'calibration')
  storage.set('ano-calibration-form', JSON.stringify({camera_provider: 'basler',
    camera_exposure_us: scenario === 'exposure' ? '' : '3200', camera_gain_db: scenario === 'gain' ? '' : '2'}));

const submitted = [];
let editPending = scenario === 'edit';
const context = vm.createContext({
  document, window: {}, Event: class {}, CustomEvent: class {}, setInterval() {},
  crypto: {randomUUID: () => 'fixture-request'},
  localStorage: {getItem: k => storage.get(k) ?? null,
    setItem: (k, v) => storage.set(k, v), removeItem: k => storage.delete(k)},
  fetch: async (route, options) => {
    if (options?.body) {
      submitted.push(JSON.parse(options.body));
      return {ok: false, status: 400, json: async () => ({error: 'End of fixture'})};
    }
    const responses = {'/api/teacher-defaults': defaults, '/api/robot-control': {defaults: {}},
      '/api/status': {owner: {active: null}}};
    assert.ok(Object.hasOwn(responses, route), route);
    if (route === '/api/teacher-defaults' && editPending) {
      editPending = false;
      field('tcp').value = '0, 0, ';
      field('setup').emit('input', field('tcp'));
      documentInput({target: field('tcp')});
    }
    return {ok: true, json: async () => responses[route]};
  }
});
vm.runInContext(read('operator-auth.js'), context, {filename: 'operator-auth.js'});
for (const file of moduleName === 'teaching' ? ['test-profile.js', 'teacher.js'] : ['app.js'])
  vm.runInContext(read(file), context, {filename: file});

setImmediate(async () => {
  if (moduleName === 'teaching') {
    assert.match(field('step-counter').textContent, /Step 1/);
    assert.equal(field('tcp').value, scenario === 'edit' ? '0, 0, ' : '0, 0, 125, 0, 0, 0');
    assert.equal(field('tcp_confirmed').checked, false);
  } else if (scenario === 'solve') {
    field('camera_exposure_us').value = '';
    await field('button-solve').onclick();
    assert.equal(submitted.length, 1);
    assert.equal(submitted[0].action, 'solve');
  } else {
    const exposure = scenario === 'exposure';
    const input = field(exposure ? 'camera_exposure_us' : 'camera_gain_db');
    await field('button-camera-preview').onclick();
    assert.equal(submitted.length, 0);
    assert.equal(input.value, '');
    assert.match(field('message').textContent, exposure ? /Exposure/ : /Gain/);
  }
});
