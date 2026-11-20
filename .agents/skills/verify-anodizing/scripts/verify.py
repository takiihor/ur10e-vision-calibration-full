#!/usr/bin/env python3
"""Internal driver for the Bubblewrap verify launcher."""

import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import time


CHECKOUT = Path('/checkout')
BOARDS = CHECKOUT / 'src/anodizing-modules/data/boards'
DATA = Path('/tmp/ui-data')
EVIDENCE = Path('/evidence')
HOST, PORT = '127.0.0.1', 8871
ROBOT_PORTS = {29999, 30001, 30002, 30003, 30004, 30020}
PAGES = {
    '/calibration': ('Calibration', 'calibration.html'),
    '/teaching': ('Marker Pick Teacher', 'teaching.html'),
    '/pick-place': ('Pick/place test', 'pick-place.html'),
    '/agv': ('AGV test', 'agv.html'),
}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def save_json(name, data):
    (EVIDENCE / name).write_text(json.dumps(data, indent=2) + '\n')


def request(path, token=None, *, payload=None):
    method = 'GET' if payload is None else 'POST'
    headers = {'X-Module-Token': token} if token else {}
    if payload is not None:
        headers['Content-Type'] = 'application/json'
    connection = http.client.HTTPConnection(HOST, PORT, timeout=3)
    try:
        connection.request(method, path, body=None if payload is None else json.dumps(payload), headers=headers)
        response = connection.getresponse()
        body = response.read()
    finally:
        connection.close()
    with (EVIDENCE / 'requests.jsonl').open('a') as log:
        log.write(json.dumps({'method': method, 'path': path, 'status': response.status,
                              **({'payload': payload} if payload is not None else {})}) + '\n')
    require(response.status == 200, f'{method} {path} returned {response.status}')
    return body


def page(path, title, filename, token):
    raw = request(path).decode()
    (EVIDENCE / filename).write_text(raw.replace(token, '<redacted>'))
    require(f'<title>{title}</title>' in raw, f'Unexpected title at {path}')
    return raw


def api(path, token):
    data = json.loads(request(path, token))
    name = path.rsplit('/', 1)[-1]
    save_json(name if name.endswith('.json') else name + '.json', data)
    return data


def listeners():
    return subprocess.check_output(['ss', '-H', '-ltnp', f'sport = :{PORT}'], text=True)


def doctor(server):
    require(server.poll() is None, 'Launcher exited before readiness. See server.log.')
    listening = listeners()
    (EVIDENCE / 'listener.txt').write_text(listening)
    require(f'{HOST}:{PORT} ' in listening and f'pid={server.pid},' in listening,
            'HTTP port is not owned by the launched server.')
    root = request('/').decode()
    token_match = re.search(r'<meta name="module-token" content="([A-Za-z0-9_-]+)">', root)
    require(bool(token_match), 'Landing page has no module token.')
    token = token_match[1]
    (EVIDENCE / 'index.html').write_text(root.replace(token, '<redacted>'))
    status = api('/api/status', token)
    owner = status['owner']
    flags = ('allow_camera', 'allow_teaching', 'allow_manual', 'allow_auto_calibration', 'allow_teacher_motion')
    require(status['app_id'] == 'anodizing_modules_v1' and status['data_dir'] == str(DATA)
            and status['robot_model']['valid'] and status['robot_model']['path'] is None
            and all(status[flag] is False for flag in flags)
            and not any(owner[field] for field in ('active', 'jobs', 'mcu_pending',
                                                   'storage_error', 'execution_uncertain', 'closing')),
            'Unexpected app, data directory, job state or hardware mode.')
    manual = api('/api/manual-pick-place', token)
    require(manual['hardware_available'] is False and manual['path_check']['status'] == 'disabled',
            'Manual hardware is available.')
    return root, token, status


def navigation(root, token, _):
    for path, (title, name) in PAGES.items():
        require(f'href="{path}"' in root, f'Missing landing link for {path}')
        page(path, title, name, token)


def check_board(relative):
    seeded = (DATA / relative).read_bytes()
    require(seeded == (BOARDS.parent / relative).read_bytes(), f'Board fixture differs: {relative}')
    return json.loads(seeded), hashlib.sha256(seeded).hexdigest()


def calibration_setup(_, token, __):
    raw = page('/calibration', *PAGES['/calibration'], token)
    relative = 'boards/calibration_stage2/board_manifest.json'
    require(f'id="manifest" value="{relative}"' in raw, 'Calibration setup did not select the bundled board.')
    board, digest = check_board(relative)
    require(any(item['name'] == 'calibration_stage2' for item in board['boards']), 'Calibration board missing.')
    save_json('fixture.json', {'path': relative, 'sha256': digest})


def teaching_setup(root, token, _):
    require('href="/teaching"' in root, 'Missing Teaching link on the landing page.')
    raw = page('/teaching', *PAGES['/teaching'], token)
    require('id="picking-board"' in raw and 'src="/teacher.js"' in raw, 'Teaching setup controls are missing.')
    defaults = api('/api/teacher-defaults', token)
    settings = defaults['settings']
    target = settings['target']
    relative = 'boards/stage2_charuco_realsense_80mm/board_manifest.json'
    manifest = str(DATA / relative)
    fixture, digest = check_board(relative)
    require(target['kind'] == 'charuco' and target['board_manifest'] == manifest
            and manifest in defaults['manifests'] and any(
                item['target'] == target and item['dimensions_mm'] == [80, 80]
                for item in defaults['picking_boards'])
            and target['board'] in {board['name'] for board in fixture['boards']},
            'Teaching did not expose a bundled 80 mm picking board.')
    require(defaults['calibrations'] == [] and settings['calibration'] == '' and settings['robot_ip'] == '',
            'Fresh Teaching setup unexpectedly has a calibration or robot target.')
    require(not (DATA / 'teaching').exists() and not list((DATA / '.jobs').glob('*.json')),
            'Read-only setup created a project or job.')
    save_json('fixture.json', {'path': relative, 'sha256': digest, 'default_target': target,
                              'project_created': False, 'job_records': []})


def pick_place_availability(_, token, __):
    page('/pick-place', *PAGES['/pick-place'], token)
    for path in ('/api/tool-power', '/api/mcu', '/api/robot-control'):
        require(api(path, token)['available'] is False, f'Hardware became available at {path}')


def tool_preview(_, token, status):
    calibration = page('/calibration', *PAGES['/calibration'], token)
    path = '/preview/tool-envelope.html'
    require(f'href="{path}"' in calibration, 'Calibration does not link to the collision editor.')
    page(path, 'Tool box draft', 'tool-preview.html', token)
    model = request('/preview/ur10e-model.json')
    digest = hashlib.sha256(model).hexdigest()
    (EVIDENCE / 'ur10e-model.json').write_bytes(model)
    scene = api('/preview/collision-scene.example.json', token)
    require(digest == status['robot_model']['sha256'] == scene['model_sha256'], 'Preview model and scene differ.')
    require(scene['confirmed'] is False and scene['environment_complete'] is False,
            'Example preview unexpectedly claims confirmation or cell coverage.')


def agv(_, token, __):
    page('/agv', *PAGES['/agv'], token)
    initial = api('/api/agv', token)
    require(initial['connected'] is False and initial['armed'] is False, 'AGV link must start disconnected.')

    def action(step, **payload):
        state = json.loads(request('/api/agv', token, payload=payload))
        save_json('agv-' + step + '.json', state)
        return state

    connected = action('connect', action='connect', mode='simulator', stations=2)
    require(connected['connected'] and connected['settings']['mode'] == 'simulator'
            and not connected['armed'], 'Expected an unarmed simulator connection.')
    queried = action('query', action='send', command='query_status')
    status = queried['status']
    require(not queried['stale'] and status['length'] == 29 and status['reply_prefix'] == '00 FF 04'
            and queried['status_hex'].startswith('00 FF 04 A5 5A 1D 25 '),
            'Expected a fresh 29-byte AGV600 reply with radio prefix 00 FF 04.')
    require(status['obstacle_input'] == 0xCC and not {'attachment_active', 'roller_speed'} & status.keys(),
            'Expected the simulator obstacle input and absent attachment fields.')
    require(queried['results']['query_status']['state'] == 'status received',
            'The valid status report did not complete the query.')
    speed = action('read-speed', action='send', command='read_speed')
    require(any(event['hex'] == '00 FF 04 A5 5A 07 21 DC 05 08'
                and event['decode'].get('read_speed_m_h') == 1500 for event in speed['log']),
            'Expected the short 1500 m/h speed reply.')
    require(speed['results']['read_speed']['state'] == 'reply: 1500 m/h',
            'The valid speed reply did not complete the read.')
    connection_id = connected['connection_id']
    require(status['max_stations'] == 1 and status['current_station'] == 1, 'Expected one learned leg from station 1.')
    armed = action('arm', action='arm', connection_id=connection_id, armed=True)
    approval = dict(connection_id=connection_id, stop_generation=armed['stop_generation'])
    action('reverse', action='send', command='reverse', **approval)
    reversed_state = action('check-reverse', action='send', command='query_status')
    require(reversed_state['status']['actual_speed_m_h'] == -1500, 'Reverse speed must be signed.')
    time.sleep(1.1)
    acknowledged = action('check-acknowledged', action='send', command='query_status')
    require(acknowledged['results']['reverse']['ack_elapsed_s'] >= 1 and any(
        entry['decode'].get('acknowledged_command') == 'reverse' for entry in acknowledged['log']),
        'Expected an acknowledged reverse command.')
    action('station-call', action='send', command='call_station', value=2, **approval)
    moving = action('check-station-target', action='send', command='query_status')
    require(moving['status']['target_station'] == 2 and not moving['status']['stopped'],
            'Expected travel toward station 2.')
    time.sleep(3.1)
    arrived = action('check-station-arrival', action='send', command='query_status')
    require(arrived['status']['current_station'] == 2 and arrived['status']['stopped']
            and arrived['results']['call_station']['state'] == 'arrived', 'Expected stopped arrival at station 2.')
    action('disarm', action='arm', connection_id=connection_id, armed=False)
    station_stop = action('station-stop', action='send', command='stop')
    require(station_stop['urgent_command'] == 'pause'
            and station_stop['log'][-1]['hex'] == '00 01 04 A5 5A 08 23 5B 01 00 86',
            'Station-call software Stop did not send Pause.')
    time.sleep(1.1)
    checked = action('check-station-stop', action='send', command='query_status')
    require(checked['status']['paused'] and checked['results']['pause'].get('confirmed_stopped') is True,
            'The simulator report did not show a stopped and paused AGV.')
    disconnected = action('disconnect', action='disconnect', connection_id=connection_id)
    require(not disconnected['connected'], 'Simulator did not disconnect.')

    standard = action('connect-standard', action='connect', mode='simulator', agv_mode=0)
    action('query-standard', action='send', command='query_status')
    basic_stop = action('standard-stop', action='send', command='stop')
    require(basic_stop['urgent_command'] == 'stop'
            and basic_stop['log'][-1]['hex'] == '00 01 04 A5 5A 08 23 87 04 00 B5',
            'Standard-mode software Stop did not send basic Stop.')
    time.sleep(1.1)
    checked = action('check-standard-stop', action='send', command='query_status')
    require(checked['status']['command_stop'] and checked['results']['stop'].get('confirmed_stopped') is True,
            'The simulator report did not corroborate basic Stop.')
    disconnected = action('disconnect-standard', action='disconnect', connection_id=standard['connection_id'])
    require(not disconnected['connected'], 'Standard-mode simulator did not disconnect.')


def check_trace(trace):
    bound = False
    accepted = 0
    for line in trace.splitlines():
        if re.search(r'sa_family=AF_INET6?\b', line) is None:
            continue
        port = re.search(r'htons\((\d+)\)', line)
        require('inet_addr("127.0.0.1")' in line and port is not None
                and int(port[1]) not in ROBOT_PORTS and 'connect(' not in line,
                f'Unexpected IP socket call: {line}')
        if 'bind(' in line and int(port[1]) == PORT and line.endswith(' = 0'):
            bound = True
        if re.search(r'\baccept4?\(', line) and re.search(r'= \d+$', line):
            accepted += 1
    require(bound and accepted, 'Trace must show the loopback bind and an accepted HTTP connection.')
    return accepted


FEATURES = {'doctor': None, 'navigation': navigation, 'calibration-setup': calibration_setup,
            'teaching-setup': teaching_setup, 'pick-place-availability': pick_place_availability,
            'tool-preview': tool_preview, 'agv': agv}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('feature', choices=FEATURES)
    parser.add_argument('host_namespace')
    args = parser.parse_args()
    namespace = os.readlink('/proc/self/ns/net')
    interfaces = [name for _, name in socket.if_nameindex()]
    require(namespace != args.host_namespace and interfaces == ['lo'], 'Refusing to start without a fresh loopback-only namespace.')
    require(not DATA.exists() and not Path('/dev/bus/usb').exists(), 'Run requires disposable state and a private /dev.')
    save_json('isolation.json', {'namespace': namespace, 'host_namespace': args.host_namespace,
                                'interfaces': interfaces, 'python': sys.version.split()[0],
                                'robot_ports': sorted(ROBOT_PORTS), 'robot_address': '192.168.12.248'})
    trace_path = EVIDENCE / 'network.strace'
    launch = ['./start-ui.sh', '--no-browser', '--port', str(PORT), '--data-dir', str(DATA)]
    command = ['strace', '-D', '-f', '-qq', '-e', 'trace=bind,listen,connect,accept,accept4',
               '-o', str(trace_path), *launch]
    env = dict(os.environ, ANODIZING_PYTHON=sys.executable, ANODIZING_OFFLINE='1',
               XDG_STATE_HOME='/tmp/xdg-state', PYTHONUNBUFFERED='1', OPENBLAS_NUM_THREADS='1')
    print('Launch:', ' '.join(launch), flush=True)
    with (EVIDENCE / 'server.log').open('w') as log:
        server = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 30
            while not listeners():
                require(server.poll() is None, 'Launcher exited before readiness. See server.log.')
                require(time.monotonic() < deadline, 'No HTTP listener after 30 seconds. See server.log.')
                time.sleep(.1)
            context = doctor(server)
            print('Doctor: owner, authenticated status and disabled hardware verified.', flush=True)
            drive = FEATURES[args.feature]
            if drive:
                drive(*context)
        finally:
            if server.poll() is None:
                server.send_signal(signal.SIGINT)
            try:
                server.wait(timeout=8)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
            remaining = listeners()
            save_json('cleanup.json', {'server_pid_in_namespace': server.pid, 'exit_code': server.returncode,
                                       'remaining_listeners': remaining})
    require(server.returncode == 0 and not remaining, 'Server did not exit cleanly.')
    accepted = check_trace(trace_path.read_text())
    save_json('result.json', {'feature': args.feature, 'result': 'pass', 'accepted_loopback_connections': accepted,
                              'outbound_ip_connects': 0, 'evidence_survives_cleanup': True})
    print(f'PASS {args.feature}: server exited and the loopback port is closed.', flush=True)


if __name__ == '__main__':
    main()
