"""Run the shipped form and profile JavaScript with offline DOM fixtures."""
from pathlib import Path
import shutil
import subprocess
import unittest


WEB = Path(__file__).parents[1] / 'anodizing_web' / 'web'


def test_menu_order_and_travel_pose_location():
    expected = ('/calibration', '/teaching', '/agv', '/pick-place')
    for name in ('index', 'calibration', 'teaching', 'agv', 'pick-place'):
        html = (WEB / (name + '.html')).read_text()
        assert sorted(expected, key=lambda path: html.index(f'href="{path}"')) == list(expected)
    assert 'Save current joints as AGV travel pose' in (WEB / 'teaching.html').read_text()
    pick = (WEB / 'pick-place.html').read_text()
    assert 'Save current joints as AGV travel pose' not in pick
    assert 'Change on Teaching' in pick


@unittest.skipUnless(shutil.which('node'), 'Node is unavailable')
class WebProfileTests(unittest.TestCase):
    def run_js(self, script, filename, *args):
        result = subprocess.run(['node', '-e', script, str(WEB / filename), *args],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_invalid_basler_values_cannot_be_submitted(self):
        self.run_js(r"""
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const fields = {camera_provider: 'basler', camera_exposure_us: '5000', camera_gain_db: '0'};
const context = {document: {querySelector: () => ({content: 'fixture'}), body: {dataset: {module: 'calibration'}},
  addEventListener() {}, getElementById: id => ({value: fields[id] ?? ''})}, localStorage: {getItem: () => null}};
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

    def check_teacher_profile(self, mode):
        self.run_js(r"""
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const mode = process.argv[2];
const ids = ['test-profile-note', 'project', 'camera_provider', 'calibration', 'validation-collision-scene',
 'validation-workspace-min', 'validation-workspace-max', 'descent-joint-min', 'descent-joint-max', 'descent-joint-source'];
const fields = Object.fromEntries(ids.map(id => [id, {id, value: '', defaultValue: '', dataset: {}}]));
fields.camera_provider.value = mode === 'basler' ? 'basler' : 'realsense';
fields['validation-workspace-max'].value = '800, 800, 1100';
let onInput;
const context = {window: {}, Event: class {}, document: {addEventListener: (type, fn) => { onInput = fn; },
 dispatchEvent: () => {},
 getElementById: id => fields[id], querySelector: () => ({content: 'fixture'})},
 fetch: async path => {
   if (path === '/api/robot-control' && mode === 'edit') {
     const field = fields['validation-workspace-min'];
     field.value = '-80, ';
     onInput({target: field});
   }
   return {ok: true, json: async () => path === '/api/teacher-defaults'
   ? {settings: {project: 'teaching/example'}}
   : {defaults: {collision_scene: 'collision/example.json', workspace_min_m: [-.9, -.9, 0],
       workspace_max_m: [.9, .9, 1.2], joint_bounds_rad: Array(6).fill([-Math.PI, Math.PI]),
       joint_bounds_source: 'reviewed fixture'}}};
 }};
vm.createContext(context); vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
context.window.applyTestProfile().then(() => {
 assert.equal(fields['validation-workspace-min'].value, mode === 'edit' ? '-80, ' : '-900, -900, 0');
 assert.equal(fields['validation-workspace-max'].value, '800, 800, 1100');
 assert.equal(fields['descent-joint-min'].value, Array(6).fill(-180).join(', '));
 assert.equal(fields['descent-joint-max'].value, Array(6).fill(180).join(', '));
 assert.equal(fields['descent-joint-source'].value, 'reviewed fixture');
}).catch(error => { console.error(error); process.exitCode = 1; });
""", 'test-profile.js', mode)

    def test_teacher_motion_fields_receive_profile_bounds(self):
        for provider in ('realsense', 'basler'):
            with self.subTest(provider=provider):
                self.check_teacher_profile(provider)

    def test_teacher_profile_preserves_an_incomplete_operator_edit(self):
        self.check_teacher_profile('edit')

    def check_form(self, module, scenario):
        result = subprocess.run(['node', str(Path(__file__).with_name('web_form.cjs')), str(WEB),
                                 module, scenario], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_teacher_handoff_uses_selected_calibration_tcp_and_requires_confirmation(self):
        for source in ('listed', 'unlisted'):
            with self.subTest(source=source):
                self.check_form('teaching', source)

    def test_teacher_initial_defaults_preserve_operator_input(self):
        self.check_form('teaching', 'edit')

    def test_saved_blank_basler_fields_block_preview_without_sending(self):
        for field in ('exposure', 'gain'):
            with self.subTest(field=field):
                self.check_form('calibration', field)

    def test_basler_solve_builds_an_offline_request_with_blank_exposure(self):
        self.check_form('calibration', 'solve')
