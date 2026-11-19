"""Loopback-only operator service. Hardware and automatic calibration are opt-in."""
import argparse
import fcntl
import hashlib
import os
import stat
import tempfile
import json
import secrets
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import calibration, teaching, pick_place, manual_pick_place, tool_power, mcu, robot_control, agv, travel_pose
from .jobs import JobOwner
from .paths import check_tree, inside

WEB = Path(__file__).parent / 'web'
MAX_BODY = 32768


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port, root, *, allow_camera=False, allow_teaching=False, allow_manual=False, allow_auto_calibration=False, robot_model=None, allow_teacher_motion=False, allow_manual_pick_place=False, allow_agv_in_recipe=False):
        lock_path = Path(tempfile.gettempdir()) / f'anodizing-modules-{os.getuid()}.lock'
        self.lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        self.data_lock_fd = None
        try:
            info = os.fstat(self.lock_fd)
            if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode):
                raise RuntimeError('The service lock has an invalid owner or type.')
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError('Another anodizing module service is already running.') from exc
            self.root = Path(root).resolve()
            self.root.mkdir(parents=True, exist_ok=True)
            job_dir = self.root / '.jobs'
            if job_dir.is_symlink():
                raise ValueError('Job directory must not be a symbolic link.')
            job_dir.mkdir(exist_ok=True)
            self.data_lock_fd = os.open(job_dir / '.owner.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            data_info = os.fstat(self.data_lock_fd)
            if data_info.st_uid != os.getuid() or not stat.S_ISREG(data_info.st_mode):
                raise RuntimeError('The data-directory lock has an invalid owner or type.')
            try:
                fcntl.flock(self.data_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError('Another service owns this data directory.') from exc
            self.model_source = None
            self.model_stat = None
            self.asset_cache = {}
            web = files('ur_handeye').joinpath('web')
            self.model_raw = web.joinpath('ur10e-model.json').read_bytes()
            if robot_model:
                self.model_source = inside(self.root, robot_model)
                if not self.model_source.is_file():
                    raise ValueError('Select a robot model file inside the data directory.')
                with self.model_source.open('rb') as stream:
                    self.model_raw = stream.read(2_000_001)
                if len(self.model_raw) > 2_000_000:
                    raise ValueError('The robot model file exceeds 2 MB.')
                source_stat = self.model_source.stat()
                self.model_stat = (source_stat.st_mtime_ns, source_stat.st_size)
            self.model_hash = hashlib.sha256(self.model_raw).hexdigest()
            self.model_data = json.loads(self.model_raw)
            self.preview_scene = json.loads(web.joinpath('collision-scene.example.json').read_bytes())
            self.preview_scene['model_sha256'] = self.model_hash
            self.preview_scene_bytes = json.dumps(self.preview_scene, allow_nan=False).encode()
            self.model_js = ('const UR10E_MODEL = ' + json.dumps(self.model_data, ensure_ascii=True, allow_nan=False, separators=(',', ':')) +
                ';\nconst UR_MODEL_SHA256 = ' + json.dumps(self.model_hash) +
                ';\nconst DEFAULT_COLLISION_SCENE = ' + json.dumps(self.preview_scene, ensure_ascii=True, allow_nan=False, separators=(',', ':')) +
                ';\nif (typeof module !== "undefined") module.exports = {UR10E_MODEL, UR_MODEL_SHA256, DEFAULT_COLLISION_SCENE};\n').encode()
            self.model_spec = None
            if self.model_source:
                from ur_handeye.collision import Scene
                Scene(self.preview_scene, model_path=self.model_source)
                self.model_spec = {'source': str(self.model_source), 'sha256': self.model_hash}
                self.check_model()
            self.owner = JobOwner(self.root, allow_auto_calibration=allow_auto_calibration, robot_model=self.model_spec, allow_teacher_motion=allow_teacher_motion, allow_manual_pick_place=allow_manual_pick_place,
                                  allow_agv_in_recipe=allow_agv_in_recipe, robot_read_access=allow_manual or allow_teaching)
            self.agv = self.owner.agv = agv.Connection(self.owner)
            self.token = secrets.token_urlsafe(32)
            self.allow_camera, self.allow_teaching, self.allow_manual = allow_camera, allow_teaching, allow_manual
            super().__init__(('127.0.0.1', port), Handler)
            port = self.server_address[1]
            self.hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}
        except BaseException:
            if self.data_lock_fd is not None:
                os.close(self.data_lock_fd)
            if self.lock_fd is not None:
                os.close(self.lock_fd)
            raise

    def check_model(self):
        if self.model_source:
            source = inside(self.root, str(self.model_source))
            stat = source.stat()
            current = (stat.st_mtime_ns, stat.st_size)
            if current == self.model_stat:
                return
            with source.open('rb') as stream:
                raw = stream.read(2_000_001)
            if len(raw) > 2_000_000 or hashlib.sha256(raw).hexdigest() != self.model_hash:
                raise ValueError('The selected robot model changed. Restart the service after review.')
            self.model_stat = current

    def cached_asset(self, path):
        """Return (bytes, mtime, sha256) without re-reading unchanged files."""
        path = Path(path)
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
        item = self.asset_cache.get(key)
        if item is None:
            data = path.read_bytes()
            item = (data, stat.st_mtime, hashlib.sha256(data).hexdigest())
            self.asset_cache[key] = item
        return item

    def server_close(self):
        super().server_close()
        self.owner.shutdown()
        self.agv.close()
        if self.data_lock_fd is not None:
            os.close(self.data_lock_fd)
            self.data_lock_fd = None
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *args):
        pass

    def reply(self, status, body, mime='application/json; charset=utf-8', *, editor_frame=False,
              cache_control='no-store', etag=None, last_modified=None):
        if not isinstance(body, bytes):
            body = json.dumps(body, allow_nan=False).encode()
        if etag and status == 200 and self.headers.get('If-None-Match') == etag:
            status, body = 304, b''
        self.send_response(status)
        headers = {'Content-Type': mime, 'Content-Length': str(len(body)),
                   'Cache-Control': cache_control, 'X-Content-Type-Options': 'nosniff',
                   'Referrer-Policy': 'no-referrer', 'Connection': 'close',
                   'Content-Security-Policy': "default-src 'self'; img-src 'self' data: blob:; "
                   "script-src 'self'; style-src 'self'; frame-ancestors " + ("'self'" if editor_frame else "'none'") + "; base-uri 'none'"}
        if etag:
            headers['ETag'] = etag
        if last_modified:
            headers['Last-Modified'] = last_modified
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True

    def send_asset(self, data, mime, *, mtime=None, etag=None, editor_frame=False,
                   cache_control='no-cache'):
        return self.reply(200, data, mime, editor_frame=editor_frame,
                          cache_control=cache_control, etag=etag or f'"{hashlib.sha256(data).hexdigest()}"',
                          last_modified=formatdate(mtime, usegmt=True) if mtime is not None else None)

    def authorized(self, *, token):
        host = self.headers.get('Host')
        origin = self.headers.get('Origin')
        if host not in self.server.hosts or origin not in (None, f'http://{host}'):
            return False
        return not token or secrets.compare_digest(self.headers.get('X-Module-Token', ''), self.server.token)

    def do_GET(self):
        path = urlsplit(self.path).path
        if not self.authorized(token=path.startswith('/api/')):
            return self.reply(403, {'error': 'Request is not authorized.'})
        if path == '/api/status':
            try:
                self.server.check_model()
                valid = True
            except (ValueError, OSError):
                valid = False
            model = {'sha256': self.server.model_hash, 'nominal': self.server.model_data.get('nominal'),
                     'path': str(self.server.model_source) if self.server.model_source else None, 'valid': valid}
            return self.reply(200, {'app_id': 'anodizing_modules_v1', 'owner': self.server.owner.status(), 'data_dir': str(self.server.root),
                                    'allow_camera': self.server.allow_camera,
                                    'allow_teaching': self.server.allow_teaching, 'allow_manual': self.server.allow_manual,
                                    'allow_auto_calibration': self.server.owner.allow_auto_calibration, 'allow_teacher_motion': self.server.owner.allow_teacher_motion, 'robot_model': model})
        if path == '/api/tool-power':
            return self.reply(200, tool_power.capability(self.server.owner))
        if path == '/api/agv-travel-pose':
            return self.reply(200, travel_pose.state(self.server.owner))
        if path == '/api/agv':
            return self.reply(200, self.server.agv.capability())
        if path == '/api/agv-robot-motion':
            from .recipe_agv import motion_state
            return self.reply(200, dict(motion_state(self.server.agv, poll=True), active=self.server.owner.active))
        if path == '/api/mcu':
            return self.reply(200, mcu.capability(self.server.owner))
        if path == '/api/robot-control':
            return self.reply(200, robot_control.capability(self.server.owner))
        if path == '/api/manual-pick-place':
            return self.reply(200, manual_pick_place.capability(self.server.owner))
        if path == '/api/teacher-state':
            try:
                query = parse_qs(urlsplit(self.path).query, strict_parsing=True)
                if set(query) != {'job'} or len(query['job']) != 1:
                    raise ValueError('Select one Teacher job.')
                return self.reply(200, teaching.session_state(self.server.owner, query['job'][0]))
            except (ValueError, TypeError) as exc:
                return self.reply(404, {'error': str(exc)})
        if path == '/api/teacher-defaults':
            from .site_profile import section
            found = teaching.defaults(self.server.root)
            settings = found['settings']
            profile = section(self.server.root, 'teacher')
            try:
                if isinstance(profile.get('project'), str) and profile['project'].strip():
                    settings['project'] = str(inside(self.server.root, profile['project'], required=False))
                if isinstance(profile.get('calibration'), str) and profile['calibration'].strip():
                    settings['calibration'] = str(inside(self.server.root, profile['calibration']))
                if isinstance(profile.get('robot_ip'), str):
                    settings['robot_ip'] = profile['robot_ip'].strip()
                if profile.get('tcp') is not None:
                    from ur_pick_place.config import vector
                    settings['tcp'] = vector(profile['tcp'], 6, 'Site profile TCP')
                if 'camera_provider' in profile:
                    provider = profile['camera_provider']
                    if provider not in ('realsense', 'basler'):
                        raise ValueError('camera_provider must be realsense or basler.')
                    settings['camera_provider'] = provider
                if isinstance(profile.get('motion_model'), str) and profile['motion_model'] in ('planar', 'full'):
                    settings['motion_model'] = profile['motion_model']
                if isinstance(profile.get('target'), dict) and profile['target'].get('board'):
                    target = dict(settings.get('target') or {}, **profile['target'])
                    if target.get('board_manifest'):
                        target['board_manifest'] = str(inside(self.server.root, target['board_manifest']))
                    settings['target'] = target
            except (OSError, ValueError, KeyError, TypeError) as exc:
                return self.reply(400, {'error': f'Teacher site profile is invalid: {exc}'})
            return self.reply(200, found)
        if path in ('/api/teacher-project', '/api/teacher-file'):
            try:
                query = parse_qs(urlsplit(self.path).query, strict_parsing=True)
                if path == '/api/teacher-project':
                    if set(query) != {'project'} or len(query['project']) != 1:
                        raise ValueError('Select one project.')
                    project = check_tree(self.server.root, inside(self.server.root, query['project'][0]))
                    saved = json.loads(inside(self.server.root, str(project / 'project.json')).read_text())
                    settings = teaching.checked_settings(self.server.root, dict(saved['settings'], project=str(project)))
                    return self.reply(200, dict(settings, agv=saved.get('agv')))
                if set(query) != {'job', 'name'} or any(len(v) != 1 for v in query.values()):
                    raise ValueError('Select one result.')
                job = self.server.owner.get(query['job'][0])
                state = job.get('result') or {}
                if not isinstance(state, dict):
                    raise ValueError('File is unavailable.')
                project = state.get('project') or {}
                name = query['name'][0]
                if job['module'] != 'teaching' or job['state'] != 'done' or not isinstance(project, dict) or name not in ('vision.json', 'grasp_pick.json', 'target.json', 'pick_target.json', 'target.script'):
                    raise ValueError('File is unavailable.')
                flow = project.get('flow') or {}
                if not flow.get('taught') or (name in ('target.json', 'pick_target.json', 'target.script') and not flow.get('exported')):
                    raise ValueError('Complete the corresponding step first.')
                with self.server.owner.lock:
                    if self.server.owner.active:
                        raise ValueError('Wait for the current job to finish.')
                    directory = check_tree(self.server.root, inside(self.server.root, project.get('project')))
                    saved = json.loads(inside(self.server.root, str(directory / 'project.json')).read_text())
                    current = saved.get('flow') or {}
                    if not current.get('taught') or (name in ('target.json', 'pick_target.json', 'target.script') and not current.get('exported')):
                        raise ValueError('Project data has changed. Complete the corresponding step again.')
                    settings = teaching.checked_settings(self.server.root, dict(saved['settings'], project=str(directory)))
                    from ur_pick_place.vision.teaching import digest
                    if saved['inputs'].get('calibration') != digest(settings['calibration']):
                        raise ValueError('Calibration changed.')
                    manifest = settings['target'].get('board_manifest')
                    if manifest and saved['inputs'].get('manifest') != digest(manifest):
                        raise ValueError('Marker manifest changed.')
                    file = inside(self.server.root, str(directory / name))
                    if not file.is_file() or file.stat().st_size > 5_000_000:
                        raise ValueError('File is unavailable or too large.')
                    content = file.read_bytes()
                return self.reply(200, content, 'text/plain; charset=utf-8' if name.endswith('.script') else 'application/json; charset=utf-8')
            except (ValueError, KeyError, TypeError, OSError) as exc:
                return self.reply(404, {'error':str(exc)})
        if path == '/api/teaching-image':
            try:
                query = parse_qs(urlsplit(self.path).query, strict_parsing=True)
                if set(query) not in ({'project'}, {'project','kind','capture_id'}) or any(len(v) != 1 for v in query.values()):
                    raise ValueError('Select one teaching project and capture.')
                project = check_tree(self.server.root, inside(self.server.root, query['project'][0]))
                if 'kind' in query:
                    owner = self.server.owner
                    with owner.lock:
                        service = owner.teacher_service
                        if query['kind'] != ['validation'] or not service or owner.teacher_project != str(project):
                            raise ValueError('This validation session is unavailable.')
                        with service.lock:
                            state = service.state()
                            capture = (state.get('project') or {}).get('validation') or {}
                            if state['busy'] or capture.get('id') != query['capture_id'][0]:
                                raise ValueError('The validation capture changed or is still being acquired.')
                            image = inside(self.server.root, str(project / 'validation.jpg'))
                            if not image.is_file() or image.stat().st_size > 5_000_000:
                                raise ValueError('Validation image is unavailable or too large.')
                            content = image.read_bytes()
                    return self.reply(200, content, 'image/jpeg')
                image = inside(self.server.root, str(project / 'capture.jpg'))
                if not image.is_file() or image.stat().st_size > 5_000_000:
                    raise ValueError('Teaching image is unavailable or too large.')
                return self.reply(200, image.read_bytes(), 'image/jpeg')
            except (ValueError, OSError) as exc:
                return self.reply(404, {'error': str(exc)})
        if path.startswith('/api/jobs/'):
            try:
                return self.reply(200, self.server.owner.get(path.removeprefix('/api/jobs/')))
            except ValueError as exc:
                return self.reply(404, {'error': str(exc)})
        preview_files = {'tool-envelope.html': 'text/html; charset=utf-8',
                         'tool-envelope.css': 'text/css; charset=utf-8',
                         'tool-envelope.js': 'text/javascript; charset=utf-8',
                         'robot-preview.js': 'text/javascript; charset=utf-8',
                         'robot-three.js': 'text/javascript; charset=utf-8',
                         'ur10e-visual.js': 'text/javascript; charset=utf-8',
                         'three-bundle.js': 'text/javascript; charset=utf-8',
                         'preview-init.js': 'text/javascript; charset=utf-8',
                         'ur10e-model.js': 'text/javascript; charset=utf-8',
                         'ur10e-model.json': 'application/json; charset=utf-8',
                         'collision-scene.example.json': 'application/json; charset=utf-8',
                         'UR-MODEL-NOTICE.txt': 'text/plain; charset=utf-8',
                         'THREE-LICENSE.txt': 'text/plain; charset=utf-8'}
        if path.startswith('/preview/'):
            name = path.removeprefix('/preview/')
            if name not in preview_files:
                return self.reply(404, {'error': 'Not found.'})
            try:
                self.server.check_model()
            except (ValueError, OSError) as exc:
                return self.reply(409, {'error': str(exc)})
            mtime = None
            if name == 'preview-init.js':
                data, mtime, _ = self.server.cached_asset(WEB / name)
            elif name == 'ur10e-model.json':
                data = self.server.model_raw
                mtime = self.server.model_source.stat().st_mtime if self.server.model_source else None
            elif name == 'ur10e-model.js' and self.server.model_source:
                data = self.server.model_js
            elif name == 'collision-scene.example.json':
                data = self.server.preview_scene_bytes
            else:
                data, mtime, _ = self.server.cached_asset(files('ur_handeye').joinpath('web', name))
                if name == 'tool-envelope.html':
                    data = data.replace(b'__TOKEN__', self.server.token.encode()).replace(b'</head>', b'<script src="preview-init.js" defer></script></head>', 1)
                if name == 'robot-preview.js' and self.server.model_source:
                    data = data.replace(b'NOMINAL MODEL \xc2\xb7 PREVIEW', b'MODEL PREVIEW')
                    data = data.replace(b"The preview shows the robot visual model and collision boxes. The nominal model uses generic UR10e data. It does not include this robot's factory calibration.",
                                        b'The preview shows the robot visual model and collision boxes for the selected model. Check the physical geometry and cell before motion.')
                    data = data.replace(b'Preview only \xc2\xb7 UR10e nominal collision hulls', b'Preview only \xc2\xb7 selected UR10e collision hulls')
            return self.send_asset(data, preview_files[name], mtime=mtime,
                                   editor_frame=(name == 'tool-envelope.html'),
                                   cache_control='no-store' if name == 'tool-envelope.html' else 'no-cache')
        routes = {'/': ('index.html', 'text/html; charset=utf-8'),
                 '/calibration': ('calibration.html', 'text/html; charset=utf-8'),
                 '/teaching': ('teaching.html', 'text/html; charset=utf-8'),
                 '/pick-place': ('pick-place.html', 'text/html; charset=utf-8'),
                 '/agv': ('agv.html', 'text/html; charset=utf-8'),
                 '/agv-motion.js': ('agv-motion.js', 'text/javascript; charset=utf-8'),
                 '/teaching-travel-pose.js': ('teaching-travel-pose.js', 'text/javascript; charset=utf-8'),
                 '/agv.js': ('agv.js', 'text/javascript; charset=utf-8'),
                 '/manual-job.js': ('manual-job.js', 'text/javascript; charset=utf-8'),
                 '/tool-power.js': ('tool-power.js', 'text/javascript; charset=utf-8'),
                 '/mcu.js': ('mcu.js', 'text/javascript; charset=utf-8'),
                 '/vertical-descent.js': ('vertical-descent.js', 'text/javascript; charset=utf-8'),
                 '/test-profile.js': ('test-profile.js', 'text/javascript; charset=utf-8'),
                 '/manual-pick-place.js': ('manual-pick-place.js', 'text/javascript; charset=utf-8'),
                 '/robot-control.js': ('robot-control.js', 'text/javascript; charset=utf-8'),
                 '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                 '/teacher.js': ('teacher.js', 'text/javascript; charset=utf-8'),
                 '/teacher-style.css': ('teacher-style.css', 'text/css; charset=utf-8'),
                 '/style.css': ('style.css', 'text/css; charset=utf-8')}
        if path not in routes:
            return self.reply(404, {'error': 'Not found.'})
        name, mime = routes[path]
        data, mtime, _ = self.server.cached_asset(WEB / name)
        if name == 'calibration.html':
            editor, _, _ = self.server.cached_asset(files('ur_handeye').joinpath('web', 'tool-envelope.html'))
            fragment = editor[editor.index(b'<section id="tool-editor"'):editor.index(b'<footer>')]
            data = data.replace(b'__TOOL_EDITOR__', fragment.replace(b'id="message"', b'id="tool-message"'))
        if name.endswith('.html'):
            for placeholder, fragment in ((b'__ROBOT_CARD__','robot-card.html'), (b'__MANUAL_JOB_PANEL__','manual-job-panel.html')):
                if placeholder in data:
                    part, _, _ = self.server.cached_asset(WEB / fragment)
                    data = data.replace(placeholder, part)
            data = data.replace(b'__TOKEN__', self.server.token.encode()).replace(b'__TEACH_TOKEN__', self.server.token.encode())
        return self.send_asset(data, mime, mtime=mtime, editor_frame=False,
                               cache_control='no-store' if name.endswith('.html') else 'no-cache')

    def do_POST(self):
        if not self.authorized(token=True):
            return self.reply(403, {'error': 'Request is not authorized.'})
        try:
            if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) != 1:
                raise ValueError('Send one bounded Content-Length.')
            if self.headers.get_content_type() != 'application/json':
                raise ValueError('Send application/json.')
            size = int(self.headers['Content-Length'])
            if not 0 < size <= MAX_BODY:
                raise ValueError('Request size is invalid.')
            body = json.loads(self.rfile.read(size), parse_constant=lambda x: (_ for _ in ()).throw(ValueError('Invalid number.')))
            if not isinstance(body, dict):
                raise ValueError('Send a JSON object.')
            path = urlsplit(self.path).path
            owner = self.server.owner
            if path == '/api/manual-control':
                from .manual_execution import control
                return self.reply(200, control(self.server.owner,body))
            if path == '/api/teacher-control':
                return self.reply(200, teaching.control(owner, body.get('job_id'), body.get('action')))
            if path == '/api/answer':
                return self.reply(200, owner.answer(body.get('job_id'), body.get('request_id'), body.get('value')))
            if path == '/api/tool-power':
                return self.reply(202, tool_power.submit(owner, body))
            if path == '/api/agv-travel-pose':
                return self.reply(200, travel_pose.save(owner, body))
            if path == '/api/agv':
                return self.reply(200, self.server.agv.submit(body))
            if path == '/api/mcu':
                return self.reply(202, mcu.submit(owner, body))
            if path not in ('/api/calibration', '/api/teaching', '/api/pick-place', '/api/manual-pick-place', '/api/robot-control'):
                return self.reply(404, {'error': 'Not found.'})
            request_id = body.pop('request_id', None)
            module = path.rsplit('/', 1)[-1]
            if module == 'calibration':
                function = lambda job_id: calibration.run(owner, self.server.root, body, job_id, self.server.allow_camera, self.server.allow_manual)
            elif module == 'teaching':
                function = lambda job_id: teaching.run(owner, self.server.root, body, job_id, self.server.allow_teaching)
            elif module == 'manual-pick-place':
                function = lambda job_id: manual_pick_place.run(self.server.root, body, job_id, owner)
            elif module == 'robot-control':
                function = lambda job_id: robot_control.run(owner, self.server.root, body, job_id)
            else:
                function = lambda job_id: pick_place.run(self.server.root, body, job_id)
            job = owner.submit(request_id, module, body, function)
            self.reply(202, job)
        except PermissionError as exc:
            self.reply(403, {'error': str(exc)})
        except (ValueError, TypeError, KeyError, OSError, json.JSONDecodeError) as exc:
            self.reply(400, {'error': str(exc)})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8870)
    parser.add_argument('--data-dir', default='data', help='Allowed IPC data directory')
    parser.add_argument('--allow-camera', action='store_true', help='Allow camera-only board capture')
    parser.add_argument('--allow-teaching', action='store_true', help='Allow read-only camera and RTDE teaching')
    parser.add_argument('--allow-manual', action='store_true', help='Allow pendant-positioned read-only calibration collection')
    parser.add_argument('--allow-auto-calibration', action='store_true', help='Allow scene-gated automatic calibration control')
    parser.add_argument('--allow-manual-pick-place', action='store_true', help='Allow reviewed manual Pick/Place, Teacher descent and finite robot-card joint steps')
    parser.add_argument('--allow-agv-in-recipe', action='store_true', help='Allow approved AGV station calls in Pick/place repeat recipes')
    parser.add_argument('--allow-teacher-motion', action='store_true', help='Allow reviewed planar-endpoint checked joint tests')
    parser.add_argument('--robot-model', help='Robot model JSON inside the data directory')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('Port must be 1 to 65535.')
    server = Server(args.port, args.data_dir, allow_camera=args.allow_camera, allow_teaching=args.allow_teaching, allow_manual=args.allow_manual, allow_auto_calibration=args.allow_auto_calibration, robot_model=args.robot_model, allow_teacher_motion=args.allow_teacher_motion, allow_manual_pick_place=args.allow_manual_pick_place, allow_agv_in_recipe=args.allow_agv_in_recipe)
    print(f'Anodizing modules: http://127.0.0.1:{args.port}/  data: {server.root}', flush=True)
    try:
        server.serve_forever(poll_interval=.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
