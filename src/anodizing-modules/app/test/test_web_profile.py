"""Run the shipped form and profile JavaScript with offline DOM fixtures."""
from pathlib import Path
import shutil
import subprocess
import unittest


WEB = Path(__file__).parents[1] / 'anodizing_web' / 'web'


@unittest.skipUnless(shutil.which('node'), 'Node is unavailable')
class WebProfileTests(unittest.TestCase):
    def run_js(self, script, filename):
        result = subprocess.run(['node', '-e', script, str(WEB / filename)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_invalid_basler_values_cannot_be_submitted(self):
        self.run_js(r"""
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const fields = {camera_provider: 'basler', camera_exposure_us: '5000', camera_gain_db: '0'};
const context = {document: {querySelector: () => ({content: 'fixture'}), body: {dataset: {module: 'calibration'}},
  getElementById: id => ({value: fields[id] ?? ''})}, localStorage: {getItem: () => null}};
vm.createContext(context);
const source = fs.readFileSync(process.argv[1], 'utf8');
vm.runInContext(source.slice(0, source.indexOf('function notice(')), context);
for (const [id, invalid] of [['camera_exposure_us', ['', 'bad', '0', '-1', 'Infinity']],
                           ['camera_gain_db', ['', 'bad', '-1', 'Infinity']]]) {
  const saved = fields[id];
  for (const value of invalid) { fields[id] = value; assert.throws(() => context.values('camera-preview'), /Exposure|Gain/); }
  fields[id] = saved;
}
assert.equal(context.values('camera-preview').camera_exposure_us, 5000);
assert.equal(context.values('camera-preview').camera_gain_db, 0);
fields.camera_provider = 'realsense'; fields.camera_exposure_us = 'bad'; fields.camera_gain_db = 'bad';
assert.doesNotThrow(() => context.values('camera'));
""", 'app.js')

    def test_teacher_motion_fields_receive_profile_bounds(self):
        self.run_js(r"""
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const ids = ['test-profile-note', 'project', 'camera_provider', 'calibration', 'validation-collision-scene',
 'validation-workspace-min', 'validation-workspace-max', 'descent-joint-min', 'descent-joint-max', 'descent-joint-source'];
const fields = Object.fromEntries(ids.map(id => [id, {value: '', defaultValue: '', dataset: {}}]));
fields['validation-workspace-max'].value = '800, 800, 1100';
const context = {window: {}, Event: class {}, document: {addEventListener: () => {}, dispatchEvent: () => {},
 getElementById: id => fields[id], querySelector: () => ({content: 'fixture'})},
 fetch: async path => ({ok: true, json: async () => path === '/api/teacher-defaults'
   ? {settings: {project: 'teaching/example'}}
   : {defaults: {collision_scene: 'collision/example.json', workspace_min_m: [-.9, -.9, 0],
       workspace_max_m: [.9, .9, 1.2], joint_bounds_rad: Array(6).fill([-Math.PI, Math.PI]),
       joint_bounds_source: 'reviewed fixture'}}})};
vm.createContext(context); vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
context.window.applyTestProfile().then(() => {
 assert.equal(fields['validation-workspace-min'].value, '-900, -900, 0');
 assert.equal(fields['validation-workspace-max'].value, '800, 800, 1100');
 assert.equal(fields['descent-joint-min'].value, Array(6).fill(-180).join(', '));
 assert.equal(fields['descent-joint-max'].value, Array(6).fill(180).join(', '));
 assert.equal(fields['descent-joint-source'].value, 'reviewed fixture');
}).catch(error => { console.error(error); process.exitCode = 1; });
""", 'test-profile.js')
