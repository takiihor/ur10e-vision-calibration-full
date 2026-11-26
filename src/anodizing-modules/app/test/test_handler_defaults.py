"""Exercise real HTTP responses without opening a listening socket."""
import email
import secrets
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from anodizing_web.server import Handler, Server


class HandlerDefaultsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.server = SimpleNamespace(root=self.root, token=secrets.token_urlsafe(32),
            hosts={'localhost'}, model_source=None, asset_cache={}, check_model=lambda: None,
            owner=SimpleNamespace(allow_manual_pick_place=False),
            line=SimpleNamespace(tanks={'Tank03': {}}),
            scene=SimpleNamespace(config={'tanks': [{'id': 'Tank03'}, {'id': 'Tank04'}]}))
        self.server.cached_asset = lambda path: Server.cached_asset(self.server, path)

    def get(self, path, extra=''):
        handler = Handler.__new__(Handler)
        handler.server = self.server
        handler.client_address = ('127.0.0.1', 12345)
        handler.rfile = io.BytesIO((f'GET {path} HTTP/1.1\r\nHost: localhost\r\n'
            f'X-Module-Token: {self.server.token}\r\n{extra}\r\n').encode())
        handler.wfile = io.BytesIO()
        handler.handle_one_request()
        head, body = handler.wfile.getvalue().split(b'\r\n\r\n', 1)
        status, headers = head.split(b'\r\n', 1)
        return int(status.split()[1]), email.message_from_bytes(headers), body

    def test_token_pages_are_not_stored_and_refresh_the_token(self):
        for path in ('/', '/calibration', '/teaching', '/pick-place', '/preview/tool-envelope.html'):
            with self.subTest(path=path):
                self.server.token = secrets.token_urlsafe(32)
                status, headers, body = self.get(path)
                self.assertEqual(status, 200)
                self.assertEqual(headers['Cache-Control'], 'no-store')
                self.assertIn(self.server.token.encode(), body)
                self.server.token = secrets.token_urlsafe(32)
                status, newer, body = self.get(path, f'If-None-Match: {headers["ETag"]}\r\n')
                self.assertEqual(status, 200)
                self.assertNotEqual(headers['ETag'], newer['ETag'])
                self.assertIn(self.server.token.encode(), body)

    def test_static_assets_revalidate_without_losing_csp(self):
        for path in ('/style.css', '/app.js', '/preview/tool-envelope.js', '/preview/tool-envelope.css'):
            with self.subTest(path=path):
                status, headers, body = self.get(path)
                self.assertEqual(status, 200)
                self.assertEqual(headers['Cache-Control'], 'no-cache')
                self.assertTrue(headers['Last-Modified'])
                self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
                status, newer, body = self.get(path, f'If-None-Match: {headers["ETag"]}\r\n')
                self.assertEqual(status, 304)
                self.assertEqual(newer['Cache-Control'], 'no-cache')
                self.assertEqual(body, b'')

    def test_invalid_teacher_profile_is_reported(self):
        (self.root / 'site-profile.json').write_text(json.dumps({'teacher': {
            'robot_ip': 'profile.example', 'tcp': [0, 0, 'invalid', 0, 0, 0]}}))
        status, headers, raw = self.get('/api/teacher-defaults')
        self.assertEqual(status, 400)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertIn('Site profile TCP', json.loads(raw)['error'])

    def test_teacher_defaults_accept_a_new_project_without_creating_it(self):
        path = self.root / 'teaching' / 'next-run'
        (self.root / 'site-profile.json').write_text(json.dumps({'teacher': {
            'project': 'teaching/next-run'}}))
        status, _, raw = self.get('/api/teacher-defaults')
        self.assertEqual(status, 200, raw)
        result = json.loads(raw)
        self.assertEqual(result['settings']['project'], str(path))
        self.assertEqual(result['tanks'], ['Tank03', 'Tank04'])
        self.assertFalse(path.parent.exists())

    def test_teacher_defaults_reject_a_project_outside_the_data_directory(self):
        (self.root / 'escape').symlink_to(self.root.parent, target_is_directory=True)
        for project in ('../next-run', 'escape/next-run', '.jobs/next-run'):
            with self.subTest(project=project):
                (self.root / 'site-profile.json').write_text(json.dumps({'teacher': {
                    'project': project}}))
                status, _, raw = self.get('/api/teacher-defaults')
                self.assertEqual(status, 400, raw)

    def test_teacher_defaults_reject_unknown_camera_provider(self):
        (self.root / 'site-profile.json').write_text(json.dumps({'teacher': {
            'camera_provider': 'baselr', 'motion_model': 'full'}}))
        status, _, raw = self.get('/api/teacher-defaults')
        self.assertEqual(status, 400, raw)
        self.assertIn('camera_provider', json.loads(raw)['error'])
