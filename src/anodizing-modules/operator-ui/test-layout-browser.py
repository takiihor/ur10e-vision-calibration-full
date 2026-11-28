#!/usr/bin/env python3
"""Check operator layouts in an isolated Firefox session against loopback fixtures.

Build the UI first with npm run build. Run this file with Python 3 and a
geckodriver on PATH (or pass --driver). No real service is contacted.
"""

import argparse
import base64
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import socket
import subprocess
import threading
import time
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


DIST = Path(__file__).resolve().parent.parent / 'app/anodizing_web/operator_dist'
ELEMENT_ID = 'element-6066-11e4-a52e-4f735466cecf'
STATES = {
    'connected': r'Connected · \d+ s',
    'stale': r'Link stale · \d+ s',
    'lost': r'Link lost(?: · \d+ s)?',
    'disconnected': r'Disconnected',
}
LINE_VIEWS = ('tank', 'signals', 'alarms', 'catalog', 'diagnostics')
ROUTES = (
    'agv', 'calibration', 'teaching', 'pick-place', 'scene', 'line', 'records', '',
    'tests/agv', 'tests/calibration', 'tests/teaching', 'tests/pick-place', 'tests/scene', 'tests/records',
    'tests/robot', 'tests/mcu', 'tests/tool-power', 'tests/line-api',
    *(f'{root}/{view}' for root in ('line', 'tests/line-api') for view in LINE_VIEWS),
)
STAMP = '2026-11-27T00:00:00Z'
LONG_ID = 'Synthetic_controller_identifier_' + '0123456789abcdef' * 10
TAG = {
    'tag_name': LONG_ID, 'group': 'Alarms', 'device_address': 'M42',
    'description': 'Synthetic upper level interlock', 'read_write': 'R', 'data_type': 'Bool',
}


class Fixtures(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def reply(self, body, kind='application/json', code=200):
        if kind == 'application/json':
            body = json.dumps(body)
        payload = body.encode()
        self.send_response(code)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def agv_state(self):
        self.server.sequence += 1
        state = self.server.state
        connected = state in ('connected', 'stale')
        report = {
            'mode': 2, 'max_stations': 3, 'stopped': False, 'actual_speed_m_h': 380,
            'current_station': 2, 'target_station': 3, 'fault_code': 0,
            'obstacle_stop': False, 'battery_percent': 79, 'position_units': 210,
            'station_arrival': False, 'command_stop': False, 'paused': False,
        }
        return {
            'sequence': self.server.sequence, 'connection_id': self.server.connection_id, 'connected': connected,
            'executor_link': True, 'executor_reachable': state != 'lost', 'job_gateway': True,
            'calibration_available': True, 'owner': 'agv-page', 'armed': False, 'stop_generation': 1,
            'settings': {'mode': 'serial', 'port': '/fixture/radio', 'address': '26 35', 'channel': '1B'},
            'serial_available': True, 'ports': [{'device': '/fixture/radio', 'description': 'Fixture radio'}],
            'radio_profile': {'address': True, 'channel': True},
            'status': report if connected else None,
            'status_age_s': 12 if state == 'stale' else 2.1 if connected else None,
            'log': [{'id': 1, 'timestamp': STAMP, 'direction': 'RX', 'hex': '01' * 96,
                     'decode': {'fixture': LONG_ID}}],
            'events': [], 'results': {}, 'commands': [],
            **({'error': 'AGV link lost. Check the connection and power.'} if state == 'lost' else {}),
        }

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/fixture':
            self.reply('<!doctype html><meta charset=utf-8><style>body{margin:0}iframe{border:0}</style>', 'text/html')
        elif path == '/dev/':
            html = (Path(self.directory) / 'index.html').read_text()
            monitor = """<script>
                window.fixtureErrors = [];
                window.addEventListener('error', event => fixtureErrors.push(event.message));
                window.addEventListener('unhandledrejection', event => fixtureErrors.push(String(event.reason)));
                const originalError = console.error;
                console.error = (...args) => { fixtureErrors.push(args.map(String).join(' ')); originalError(...args); };
            </script>"""
            self.reply(html.replace('__TOKEN__', '').replace('__SIMULATION__', 'on')
                       .replace('<head>', '<head>' + monitor), 'text/html')
        elif path == '/api/agv':
            self.reply(self.agv_state())
        elif path == '/api/status':
            self.reply({
                'app_id': 'anodizing_modules_v1', 'owner': {'jobs': [], 'stop_targets': []},
                'operator_auth': {'required': True, 'authorized': True},
                'allow_teaching': True, 'allow_manual': True, 'allow_manual_pick_place': True,
                **({'line_link': {'state': 'lost', 'since': STAMP}}
                   if self.server.state == 'lost' else {}),
            })
        elif path == '/api/state':
            self.reply({
                'agv': {'connection_id': self.server.connection_id, 'sequence': self.server.sequence},
                'variables': [{'id': 'line.' + LONG_ID, 'name': 'Synthetic upper level interlock',
                               'value': 1, 'quality': 'fresh'}],
                'line': {'mode': 'wired', 'quality': 'fresh', 'catalog_revision': 1,
                         'tanks': {'Tank01': {'basket': LONG_ID}}, 'system_signals': {'remote_mode': LONG_ID}},
            })
        elif path == '/api/agv-bluetooth':
            self.reply({
                'fields': [{'field': 'max_stations', 'name': 'Learned stations', 'raw': '0300', 'meaning': '3 station calls'}],
                'route': {'counts': [1, 0], 'records': {'outbound': ['A1' * 124], 'return': []}},
            })
        elif path == '/api/line/catalog':
            self.reply({'revision': 1, 'tags': [TAG], 'tanks': {'Tank01': {'basket': LONG_ID}}, 'warnings': []})
        elif path == '/api/line/diagnostics':
            self.reply({'events': [{'at': STAMP, 'operation': 'Read', 'outcome': LONG_ID}]})
        elif path == '/api/scene':
            self.reply({'tanks': [{'id': 'Tank01', 'label': 'Synthetic long tank name ' + LONG_ID,
                                  'status_id': 'line.' + LONG_ID}]})
        elif path == '/api/records':
            self.reply({'records': [{'id': LONG_ID, 'status': 'unreadable'}]})
        elif path.startswith('/api/'):
            self.reply({})
        elif path == '/favicon.ico':
            self.send_response(204)
            self.end_headers()
        elif path.startswith('/dev/'):
            self.path = path.removeprefix('/dev')
            super().do_GET()
        else:
            self.send_error(404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if self.path == '/api/agv' and body.get('action') == 'connect':
            self.server.connection_id += 1
            self.server.state = 'connected'
            self.reply(self.agv_state())
        elif self.path == '/api/agv' and body.get('action') == 'send' and body.get('command') == 'stop':
            self.server.stops += 1
            self.reply(self.agv_state())
        else:
            self.server.unexpected.append((self.path, body.get('action')))
            self.reply({'error': 'Write rejected by layout fixture'}, code=405)


def wait_for(action, description):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            if value := action():
                return value
        except URLError:
            pass
        time.sleep(.05)
    raise AssertionError(description)


class Firefox:
    def __init__(self, driver, binary, dpr):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        self.url = f'http://127.0.0.1:{port}'
        self.process = subprocess.Popen([driver, '--port', str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.session = ''
        try:
            wait_for(lambda: self.request('/status'), 'geckodriver did not start')
            options = {'args': ['-headless'], 'prefs': {'layout.css.devPixelsPerPx': str(dpr)}}
            if binary:
                options['binary'] = binary
            value = self.request('/session', {
                'capabilities': {'alwaysMatch': {'browserName': 'firefox', 'moz:firefoxOptions': options}},
            })
            self.session = '/session/' + value['sessionId']
        except BaseException:
            self.close()
            raise

    def request(self, path, data=None, method=None):
        request = Request(self.url + path, None if data is None else json.dumps(data).encode(),
                          {'Content-Type': 'application/json'}, method=method)
        with urlopen(request, timeout=30) as response:
            return json.load(response)['value']

    def call(self, path, data=None, method=None):
        return self.request(self.session + path, data, method)

    def js(self, script, *args):
        return self.call('/execute/sync', {'script': script, 'args': args})

    def close(self):
        try:
            if self.session:
                self.call('', method='DELETE')
        finally:
            self.process.terminate()
            self.process.wait(timeout=10)

    def open(self, origin, width, height, route):
        self.call('/url', {'url': origin + '/fixture'})
        self.js('''
            const frame = document.createElement('iframe');
            frame.id = 'fixture';
            frame.width = arguments[0];
            frame.height = arguments[1];
            frame.src = '/dev/#/' + arguments[2];
            document.body.append(frame);
        ''', width, height, route)
        frame = self.call('/element', {'using': 'css selector', 'value': '#fixture'})
        self.call('/frame', {'id': frame})
        return frame

    def screenshot(self, frame, path):
        self.call('/frame', {'id': None})
        value = self.call('/element/' + frame[ELEMENT_ID] + '/screenshot')
        path.write_bytes(base64.b64decode(value))
        self.call('/frame', {'id': frame})


def run(browser, server, args):
    origin = f'http://127.0.0.1:{server.server_port}'
    errors = []

    def check(condition, description):
        if not condition:
            errors.append(description)
            print('FAIL', description, flush=True)

    def page_fits(description):
        width, scroll, clipped = browser.js('''
            const root = document.documentElement;
            return [root.clientWidth, root.scrollWidth, [...document.querySelectorAll('body *')]
                .filter(e => e.getBoundingClientRect().right > root.clientWidth + 1)
                .slice(0, 8).map(e => [e.tagName, e.id || e.className, Math.round(e.getBoundingClientRect().right)])];
        ''')
        check(scroll <= width, f'{description}: document scrollWidth {scroll} > clientWidth {width}; {clipped}')

    def stop_uncovered(description):
        check(browser.js('''const stop = document.querySelector('#agv-stop');
            const r = stop.getBoundingClientRect();
            return stop.contains(document.elementFromPoint((r.left + r.right) / 2, (r.top + r.bottom) / 2));'''),
            f'{description}: overlay covers Stop')

    for width in args.widths:
        browser.call('/window/rect', {'width': max(width + 64, 530), 'height': args.height + 128})
        compact = width <= 900
        for state, label in STATES.items():
            if state not in args.states:
                continue
            routes = ROUTES[:1] if args.quick else ROUTES
            for route in routes:
                context = f'{state}, {width}px, {route or "home"}'
                connect = route == 'agv' and state == 'connected'
                server.state = 'disconnected' if connect else state
                frame = browser.open(origin, width, args.height, route)
                try:
                    wait_for(lambda: browser.js("return document.querySelector('#agv-link')?.innerText"), context)
                    if connect:
                        if compact:
                            browser.js("document.querySelector('[data-ui=\"safety.controls.open\"]').click()")
                        wait_for(lambda: browser.js('''
                            const button = document.querySelector('[data-ui="agv.link.connect"]');
                            if (!button || button.disabled) return false;
                            button.click();
                            return true;
                        '''), f'{context}: fixture Connect never enabled')
                        if compact:
                            browser.js("document.querySelector('.p-drawer-close-button').click()")
                    if state != 'disconnected':
                        wait_for(lambda: browser.js("return document.querySelector('#agv-link')?.innerText !== 'Disconnected'"),
                                 f'{context}: fixture link did not update')
                    if route == 'agv':
                        browser.js("document.querySelector('[role=tab][data-ui=\"agv.bluetooth.wiredtab\"]')?.click()")
                    observed = browser.js("return [innerWidth, devicePixelRatio, document.querySelector('#agv-link').innerText]")
                    check(observed[0] == width, f'{context}: viewport was {observed[0]}px')
                    check(observed[1] == float(args.dpr), f'{context}: DPR was {observed[1]}')
                    check(re.fullmatch(label, observed[2]), f'{context}: badge was {observed[2]!r}')
                    page_fits(context)
                    layout = browser.js('''
                        const panel = document.querySelector('[data-ui="safety.panel"]');
                        const stop = document.querySelector('#agv-stop');
                        const title = document.querySelector('#operator-page-heading');
                        const link = document.querySelector('#agv-link');
                        const drawer = document.querySelector('[data-ui="safety.controls.open"]');
                        const box = e => e?.getBoundingClientRect().toJSON();
                        return {panel: box(panel), stop: box(stop), title: box(title), link: box(link),
                            drawer: !!drawer?.getClientRects().length,
                            contains: !!panel && panel.contains(stop) && panel.contains(link),
                            position: panel && getComputedStyle(panel).position};
                    ''')
                    check(layout['contains'], f'{context}: Stop and link must be in safety panel: {layout}')
                    stop_box = layout['stop']
                    check(stop_box and 0 <= stop_box['left'] < stop_box['right'] <= width
                          and 0 <= stop_box['top'] < stop_box['bottom'] <= args.height,
                          f'{context}: Stop is outside viewport {stop_box}')
                    if not compact:
                        panel, title = layout['panel'], layout['title']
                        check(panel and title and 300 <= panel['width'] <= 340 and title['right'] <= panel['left']
                              and layout['position'] in ('sticky', 'fixed') and not layout['drawer'],
                              f'{context}: expected sticky right control column and left page: {layout}')
                        check(browser.js('''const p = document.querySelector('[data-ui="safety.panel"]');
                            return ['#agv-pause', arguments[0]].every(s => p?.contains(document.querySelector(s)));''',
                            '#agv-disconnect' if state in ('connected', 'stale') else '#agv-connect'),
                            f'{context}: Pause and applicable link action must be in column')
                    else:
                        check(layout['drawer'], f'{context}: compact Controls button missing')
                        if layout['drawer']:
                            browser.js("document.querySelector('[data-ui=\"safety.controls.open\"]').click()")
                            check(browser.js("return !!document.querySelector('.p-drawer')?.getClientRects().length"),
                                  f'{context}: Controls did not open a Drawer')
                            page_fits(f'{context}, opening Controls')
                            wait_for(lambda: browser.js('''const r = document.querySelector('.p-drawer')?.getBoundingClientRect();
                                return r && r.right <= innerWidth + 1;'''), f'{context}: Drawer did not settle')
                            stop_uncovered(f'{context}, Controls open')
                    if not compact:
                        actions = browser.js('''return ['connect', 'disconnect'].filter(name =>
                            document.querySelector(`#agv-${name}`)?.getClientRects().length);''')
                        expected = ['disconnect'] if state in ('connected', 'stale') else ['connect']
                        check(actions == expected, f'{context}: visible link actions {actions}, expected {expected}')
                    if route in ('agv', 'calibration', 'pick-place'):
                        check(browser.js('''const p = document.querySelector(arguments[0]);
                            return !!p?.contains(document.querySelector('[data-ui="agv.owner.panel"]'));''',
                            '[data-ui="safety.panel"]' if not compact else '.p-drawer'),
                            f'{context}: ownership card is not in controls')
                    if route == 'agv':
                        widths = browser.js('''return ['#agv-pause', '#agv-connect, #agv-disconnect',
                            '#agv-job-owner-agv', '#agv-job-owner-calibration', '#agv-job-owner-release']
                            .map(selector => document.querySelector(selector)?.getBoundingClientRect().width).filter(Boolean);''')
                        check(len(widths) == 5 and max(widths) - min(widths) < 1,
                              f'{context}: expected equal control widths, got {widths}')
                    if route == 'agv' and state == 'lost':
                        alarms = browser.js('''return [...document.querySelectorAll('#agv-alarms .p-message, #agv-link-loss')]
                            .map(e => e.innerText.trim()).filter(text => /AGV link lost/i.test(text));''')
                        check(len(alarms) == 1 and re.search(r'\d{1,2}:\d{2}', alarms[0]),
                              f'{context}: expected one timestamped AGV link loss message, got {alarms}')
                        hint = 'Check the connection and power.'
                        check(len(alarms) == 1 and hint in alarms[0] and alarms[0].count(hint) == 1,
                              f'{context}: missing or repeated guidance in link message: {alarms}')
                        text = browser.js('''return [...document.querySelectorAll('#agv-link-loss, #agv-connect-why, #agv-armed-why')]
                            .map(e => e.innerText).join(' ');''')
                        check(text.count(hint) == 1, f'{context}: expected one visible copy of link guidance')
                        check(browser.js('''const button = document.querySelector('#agv-connect');
                            const arm = document.getElementById('agv-armed');
                            return button.disabled && arm.disabled && [button, arm].every(control =>
                                control.getAttribute('aria-describedby').split(' ').includes('agv-link-loss'));'''),
                            f'{context}: blocked controls should reference the shared explanation')
                    if route == 'agv' and state == 'disconnected' and width in (1366, 1920):
                        toggle = browser.call('/element', {'using': 'css selector', 'value': '[data-ui="nav.more.toggle"]'})
                        browser.js("document.querySelector('[data-ui=\"nav.more.toggle\"]').focus()")
                        browser.call(f'/element/{toggle[ELEMENT_ID]}/value', {'text': '\uE007'})
                        menu = browser.js('''return !!document.querySelector('[role=menu]')?.getClientRects().length''')
                        check(menu, f'{context}: Enter did not open More as a menu')
                        if menu:
                            stop_uncovered(f'{context}, More open')
                            if args.screenshots:
                                browser.screenshot(frame, args.screenshots / f'more-menu-{width}.png')
                            stops_before = server.stops
                            active = browser.call('/element/active')
                            browser.call(f'/element/{active[ELEMENT_ID]}/value', {'text': '\uE00C'})
                            wait_for(lambda: server.stops > stops_before, f'{context}: Escape did not send Stop')
                            wait_for(lambda: browser.js("return !document.querySelector('[role=menu]')?.getClientRects().length"),
                                     f'{context}: Escape did not close More')
                            focus = browser.js('''return [!!document.querySelector('[role=menu]')?.getClientRects().length,
                                document.activeElement === document.querySelector('[data-ui="nav.more.toggle"]')]''')
                            check(focus == [False, True], f'{context}: Escape did not close More and restore focus: {focus}')
                    if state == 'connected' and route == 'agv':
                        outcome = browser.js("return document.querySelector('#agv-outcome')?.innerText || ''")
                        check('Connected · Serial AGV' not in outcome, f'{context}: duplicate Stop feedback {outcome!r}')
                    if route == 'agv':
                        if not args.quick:
                            pause_reason = browser.js('''
                                const pause = document.querySelector('#agv-pause');
                                const tooltip = document.getElementById('agv-pause-why');
                                return pause.disabled && tooltip?.textContent.trim() && pause.getAttribute('aria-describedby')
                                    ?.split(' ').includes(tooltip.id);
                            ''')
                            check(pause_reason, f'{context}: disabled Pause lacks its accessible reason')
                            if width == 390:
                                visible_tooltip = browser.js('''
                                    document.querySelector('.pause-help')?.focus();
                                    return getComputedStyle(document.getElementById('agv-pause-why')).visibility === 'visible';
                                ''')
                                check(visible_tooltip, f'{context}: Pause reason is hidden on keyboard focus')
                                page_fits(f'{context}, focused Pause reason')
                                browser.js('document.activeElement.blur()')
                        if state in ('connected', 'stale'):
                            detail = browser.js("return document.querySelector('#agv-link').getAttribute('aria-label')")
                            check('last status report' in (detail or ''),
                                  f'{context}: link tooltip lost the report detail: {detail!r}')
                        if width == 1366:
                            pinned = browser.js('''return Math.max(...[...document.querySelectorAll('header')]
                                .filter(e => ['fixed', 'sticky'].includes(getComputedStyle(e).position))
                                .map(e => e.getBoundingClientRect().bottom), 0)''')
                            print(f'{context}: pinned topbar bottom {pinned}px (baseline 114px)', flush=True)
                        if args.screenshots and width in (1366, 1854, 1920, 390) and state in ('connected', 'lost', 'disconnected'):
                            browser.js('''document.querySelector('.p-drawer-close-button')?.click();
                                document.querySelector('main [role=tablist] [role=tab]')?.click();''')
                            if compact:
                                wait_for(lambda: browser.js("return !document.querySelector('.p-drawer')"),
                                         f'{context}: Drawer did not close')
                            browser.screenshot(frame, args.screenshots / f'agv-{state}-{width}.png')
                    if not args.quick:
                        check_panels(browser, route, context, page_fits, check,
                                     width in (320, 1280) and state == 'connected' and not route.startswith('tests/'))
                    js_errors = browser.js('return window.fixtureErrors')
                    check(not js_errors, f'{context}: JavaScript errors {js_errors}')
                    print(f'Checked {context} at DPR {observed[1]}', flush=True)
                finally:
                    browser.js("['agv', 'teaching', 'pick-place'].forEach(page => localStorage.removeItem('operator-tab.' + page))")
                    browser.call('/frame', {'id': None})

    check(not server.unexpected, f'Unexpected fixture writes: {server.unexpected}')
    if errors:
        raise SystemExit(f'{len(errors)} browser layout checks failed')


def check_panels(browser, route, context, page_fits, check, keyboard):
    page = route.removeprefix('tests/').split('/')[0]
    if page == '':
        check(browser.js("return !!document.querySelector('[data-ui=\"home.page\"]')"), f'{context}: Home is missing')
        return
    if page == 'calibration':
        steps = browser.js("return [...document.querySelectorAll('[data-ui=\"calibration.step.select\"]')].map(e => e.id)")
        check(len(steps) >= 2, f'{context}: Calibration step controls are missing')
        for step in steps:
            browser.js('document.getElementById(arguments[0]).click()', step)
            page_fits(f'{context}, {step}')
        return
    if page in ('line', 'line-api', 'records', 'scene'):
        if page.startswith('line'):
            selector = ('[data-ui="line.log.row"]' if route.endswith('diagnostics')
                        else f'.line-readings li[data-tag="{LONG_ID}"]')
        else:
            selector = ('[data-ui="recorder.history.unreadable.row"]' if page == 'records'
                        else '[data-ui="scene.labels.row"][data-ui-key="Tank01"]')
        wait_for(lambda: browser.js('return document.querySelector(arguments[0]) !== null', selector),
                 f'{context}: fixture content did not load')
        page_fits(f'{context}, fixture data')
        return
    ids = browser.js("return [...document.querySelectorAll('main [role=tablist] [role=tab]')].map(e => e.id)")
    check(len(ids) >= (6 if page == 'agv' else 2), f'{context}: missing in-page tabs: {ids}')
    if page == 'agv' and ids and not context.startswith('disconnected,'):
        selector = ('agv.tabs.stations' if context.startswith('connected,') else 'agv.tabs.connection')
        warning = browser.js('''
            return document.querySelector('[data-ui="' + arguments[0] + '"] .p-badge')?.textContent.trim();
        ''', selector)
        check(warning == 'Warning', f'{context}: no warning badge on {selector}')
    if keyboard and len(ids) >= 2:
        tab = browser.call('/element', {'using': 'css selector', 'value': f'[id="{ids[0]}"]'})
        first = browser.js("return document.querySelector('main [role=tab][aria-selected=true]')?.id")
        check(first == ids[0], f'{context}: Overview was not initially selected')
        browser.js('document.getElementById(arguments[0]).click()', ids[0])
        arrow_right = '\uE014'
        browser.call(f'/element/{tab[ELEMENT_ID]}/value', {'text': arrow_right})
        focus = browser.js('''return [document.activeElement.id, document.activeElement.matches(':focus-visible'),
            getComputedStyle(document.activeElement).outlineStyle !== 'none' || getComputedStyle(document.activeElement).boxShadow !== 'none'];''')
        check(focus == [ids[1], True, True], f'{context}: keyboard tab focus {focus}')
        if context.startswith('connected, 320px'):
            focused = browser.call('/element/active')
            browser.call(f'/element/{focused[ELEMENT_ID]}/value', {'text': '\uE010'})
            last = browser.js('''const tab = document.activeElement;
                const box = tab.closest('.p-tablist-content').getBoundingClientRect(), item = tab.getBoundingClientRect();
                return [tab.id, item.left >= box.left - 1 && item.right <= box.right + 1];''')
            check(last == [ids[-1], True], f'{context}: last focused tab was clipped: {last}')
    page_loaded_at = browser.js('return performance.timeOrigin')
    for tab in ids:
        outcome = browser.js('''
            const tab = document.getElementById(arguments[0]);
            tab.click();
            return [tab.textContent.trim(), location.hash];
        ''', tab)
        title, route_hash = outcome
        check(route_hash == '#/' + route, f'{context}, {title}: route changed to {route_hash}')
        visible, page_exists = browser.js('''
            const tab = document.getElementById(arguments[0]);
            const panel = document.getElementById(tab.getAttribute('aria-controls'));
            return [tab.getAttribute('aria-selected') === 'true'
                && panel?.getAttribute('role') === 'tabpanel' && panel.getBoundingClientRect().height > 0,
                performance.timeOrigin === arguments[1]];
        ''', tab, page_loaded_at)
        check(visible, f'{context}, {title}: selected tab has no visible linked tabpanel')
        check(page_exists, f'{context}, {title}: tab selection reloaded the page')
        page_fits(f'{context}, {title}')
        if page == 'agv':
            for selector in ('#agv-stop', '#agv-link', '[data-ui="agv.status.panel"]'):
                visible = browser.js("return !!document.querySelector(arguments[0])?.getClientRects().length", selector)
                check(visible, f'{context}, {title}: {selector} is hidden')
            for toggle, content in (
                ('agv.bluetooth.recordstoggle', 'agv.bluetooth.record'),
                ('agv.status.details.toggle', 'agv.status.detail.row'),
                ('agv.record.commands.toggle', 'agv.record.commands.table'),
                ('agv.record.frames.toggle', 'agv.record.frames.table'),
            ):
                selector = f'[data-ui="{toggle}"]'
                if 'bluetooth' in toggle and 'Bluetooth' in title:
                    wait_for(lambda: browser.js('return !!document.querySelector(arguments[0])', selector),
                             f'{context}: Bluetooth station fixture did not load')
                opened = browser.js('''
                    const toggle = document.querySelector(arguments[0]);
                    if (!toggle?.getClientRects().length) return false;
                    if (!toggle.closest('details')?.open && toggle.getAttribute('aria-expanded') !== 'true') toggle.click();
                    return true;
                ''', selector)
                if opened and (toggle != 'agv.status.details.toggle' or context.startswith(('connected,', 'stale,'))):
                    wait_for(lambda: browser.js(
                        'return !!document.querySelector(arguments[0])?.getClientRects().length', f'[data-ui="{content}"]'),
                        f'{context}: {content} did not render')
                    page_fits(f'{context}, {toggle}')
        if keyboard and tab == ids[-1]:
            browser.js('window.fixtureBeforeReload = true; location.reload()')
            remembered = wait_for(lambda: browser.js("""
                return !window.fixtureBeforeReload && document.querySelector('main [role=tab][aria-selected=true]')?.id;
            """), f'{context}: no selected tab after reload')
            check(remembered == tab, f'{context}: last tab was not remembered after reload')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dist', type=Path, default=DIST)
    parser.add_argument('--driver', default='geckodriver')
    parser.add_argument('--binary')
    parser.add_argument('--dpr', default='1')
    parser.add_argument('--widths', type=int, nargs='+', default=[1280, 1366, 1920, 390])
    parser.add_argument('--height', type=int, default=768)
    parser.add_argument('--states', nargs='+', choices=STATES, default=list(STATES))
    parser.add_argument('--screenshots', type=Path)
    parser.add_argument('--quick', action='store_true')
    args = parser.parse_args()
    if args.screenshots:
        args.screenshots.mkdir(parents=True, exist_ok=True)
    with ThreadingHTTPServer(('127.0.0.1', 0), partial(Fixtures, directory=args.dist)) as server:
        server.sequence = 0
        server.connection_id = 1
        server.state = 'disconnected'
        server.unexpected = []
        server.stops = 0
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            browser = Firefox(args.driver, args.binary, args.dpr)
            try:
                run(browser, server, args)
            finally:
                browser.close()
        finally:
            server.shutdown()
            thread.join()


if __name__ == '__main__':
    main()
