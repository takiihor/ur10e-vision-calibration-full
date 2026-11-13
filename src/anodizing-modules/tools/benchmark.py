#!/usr/bin/env python3
"""Benchmark the default offline module service over its loopback HTTP endpoint."""
import argparse
import json
import math
import re
import statistics
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def check(condition, message):
    if not condition:
        raise RuntimeError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8870)
    parser.add_argument('--output', type=Path, required=True, help='New local JSON result file')
    parser.add_argument('--source-id', required=True, help='Fixture-supplied source identity; not independently verified')
    parser.add_argument('--fixture', required=True, help='Fixture description and host arrangement')
    parser.add_argument('--warmups', type=int, default=1)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--timeout', type=float, default=300, help='Maximum seconds per job')
    parser.add_argument('--picking-board', help='Discovered board name; default is the first 80 mm picking board')
    args = parser.parse_args()
    check(1 <= args.port <= 65535 and args.warmups >= 0 and args.repeats >= 1 and args.timeout > 0,
          'Invalid port, repetition count or timeout')
    check(not args.output.exists(), 'Output file already exists')
    base = f'http://127.0.0.1:{args.port}'
    with urlopen(base + '/', timeout=5) as response:
        page = response.read(100000).decode()
    match = re.search(r'<meta name="module-token" content="([A-Za-z0-9_-]+)">', page)
    check(match is not None, 'Expected module service was not found')
    token = match.group(1)

    def request(path, data=None):
        headers = {'X-Module-Token': token}
        payload = None
        if data is not None:
            headers['Content-Type'] = 'application/json'
            payload = json.dumps(data, allow_nan=False).encode()
        try:
            with urlopen(Request(base + path, data=payload, headers=headers), timeout=10) as response:
                return json.load(response)
        except HTTPError as exc:
            raise RuntimeError(f'HTTP {exc.code}: {exc.read().decode(errors="replace")}') from exc

    def status():
        start = time.monotonic()
        value = request('/api/status')
        latency = time.monotonic() - start
        check(value.get('app_id') == 'anodizing_modules_v1' and
              all(value.get(k) is False for k in ('allow_camera', 'allow_teaching', 'allow_manual', 'allow_auto_calibration','allow_teacher_motion')),
              'Service must use default offline capabilities')
        check(not value['owner']['active'] and not value['owner']['storage_error'], 'Service is busy or job storage failed')
        return latency

    def job(module, body, expected='done'):
        request_id = uuid.uuid4().hex
        start = time.monotonic()
        accepted = request('/api/' + module, dict(body, request_id=request_id))
        accepted_time = time.monotonic()
        check(accepted.get('id') == request_id, 'Unexpected accepted job ID')
        deadline = start + args.timeout
        while True:
            result = request('/api/jobs/' + request_id)
            if result['state'] in ('done', 'failed', 'interrupted'):
                break
            check(time.monotonic() < deadline, f'Job {request_id} timed out')
            time.sleep(.1)
        finished_time = time.monotonic()
        check(result['state'] == expected, f"Job {request_id}: {result['state']}: {result.get('error')}")
        check(result.get('finished') is not None and result.get('created') is not None, 'Server timing unavailable')
        return result, {'id': request_id, 'state': result['state'], 'error': result.get('error'),
                        'server_created_unix_s': result['created'], 'server_finished_unix_s': result['finished'],
                        'server_elapsed_s': result['finished'] - result['created'],
                        'client_accept_s': accepted_time - start, 'client_complete_s': finished_time - start}

    run_id = 'benchmark-' + uuid.uuid4().hex
    report = {'schema_version': 1, 'started_utc': datetime.now(timezone.utc).isoformat(),
              'source_identity_supplied': args.source_id, 'fixture_supplied': args.fixture,
              'source_identity_verified_by_client': False, 'run_id': run_id,
              'simulation_only': True, 'warmups': args.warmups, 'repeats': args.repeats,
              'timing_note': 'Client monotonic durations include HTTP and polling. Server elapsed uses server wall clock; no cross-host clock subtraction.',
              'iterations': []}
    # Open exclusively before work so a failure preserves partial measurements.
    with args.output.open('x', encoding='utf-8') as output:
        def save():
            output.seek(0)
            json.dump(report, output, indent=2, allow_nan=False)
            output.write('\n')
            output.truncate()
            output.flush()
        try:
            defaults = request('/api/teacher-defaults')
            choices = [item for item in defaults['picking_boards']
                       if item.get('target', {}).get('kind') == 'charuco'
                       and 'calibration' not in str(item['target'].get('board', '')).lower()]
            if args.picking_board:
                choices = [item for item in choices if item['target'].get('board') == args.picking_board]
            else:
                choices = [item for item in choices
                           if all(abs(value - 80) < 1e-9 for value in item.get('dimensions_mm', []))] or choices
            board = choices[0]['target'] if choices else None
            check(board is not None, 'No discovered picking board is available; pass --picking-board')
            for index in range(args.warmups + args.repeats):
                item = {'index': index, 'warmup': index < args.warmups, 'jobs': {}}
                report['iterations'].append(item)
                save()
                item['status_latency_s'] = status()
                prefix = f'benchmark/{run_id}/iteration-{index:03d}'
                iteration_start = time.monotonic()
                calibration, timing = job('calibration', {'action': 'simulate', 'session': prefix + '/calibration',
                    'manifest': 'boards/calibration_stage2/board_manifest.json', 'board': 'calibration_stage2',
                    'views': 14, 'tilt_deg': 25, 'roll_deg': 30})
                item['jobs']['calibration'] = timing
                result = calibration.get('result') or {}
                native = result.get('result') or {}
                calibration_path = result.get('path')
                check(isinstance(calibration_path, str) and calibration_path and native.get('type') == 'eye_in_hand' and
                      native.get('verdict') in ('good', 'acceptable') and
                      (native.get('simulation_truth') or native.get('camera', {}).get('simulated') or native.get('robot', {}).get('host') == 'simulated'),
                      'Calibration result is missing, unsuitable or not simulated')
                item['calibration'] = {'ipc_path': calibration_path, 'verdict': native['verdict'],
                                       'sample_count': native.get('metrics', {}).get('sample_count'),
                                       'simulated': True}
                teaching, timing = job('teaching', {'action': 'simulate', 'project': prefix + '/teaching',
                                                   'calibration': calibration_path, 'target': board})
                item['jobs']['teaching'] = timing
                taught = teaching['result']
                check(taught.get('simulation') is True and isinstance(taught.get('project'), str) and
                      re.fullmatch(r'[0-9a-f]{64}', taught.get('calibration_sha256', '')),
                      'Teaching result lacks simulated project or calibration identity')
                item['teaching'] = taught
                common = {'project': taught['project'], 'calibration': calibration_path, 'manifest': board['board_manifest']}
                initial, timing = job('pick-place', dict(common, operation='PLACE', shift_mm=[0, 0]), expected='failed')
                item['jobs']['initial_place_refusal'] = timing
                check('Run simulated PICK before PLACE' in (initial.get('error') or ''), 'Initial held state was not empty')
                pick, timing = job('pick-place', dict(common, operation='PICK', shift_mm=[0, 0]))
                item['jobs']['pick'] = timing
                place, timing = job('pick-place', dict(common, operation='PLACE', shift_mm=[30, 0]))
                item['jobs']['place'] = timing
                pick, place = pick['result'], place['result']
                check(pick.get('simulation') is True and place.get('simulation') is True and
                      pick.get('operation') == 'PICK' and place.get('operation') == 'PLACE' and
                      pick.get('held_simulated') is True and place.get('held_simulated') is False and
                      'simulated' in pick.get('outcome', '') and 'simulated' in place.get('outcome', ''),
                      'Unexpected simulated PICK/PLACE outcome')
                check(pick.get('result_path') and place.get('result_path') and pick['result_path'] != place['result_path'] and
                      place['observation_time'] > pick['observation_time'], 'Results or synthetic observations are not distinct')
                p, q = pick['target_tcp_pose_m_rad'], place['target_tcp_pose_m_rad']
                check(len(p) == len(q) == 6 and all(type(v) in (int, float) and math.isfinite(v) for v in p + q) and
                      abs(q[0] - p[0] - .03) < 1e-8 and all(abs(q[i] - p[i]) < 1e-8 for i in range(1, 6)),
                      'Shifted PLACE target differs from expected 30 mm base-X translation')
                item.update(held_simulated=[False, True, False], pick=pick, place=place,
                            target_delta_m_rad=[q[i] - p[i] for i in range(6)],
                            elapsed_s=time.monotonic() - iteration_start, outcome='passed')
                save()
            measured = [item for item in report['iterations'] if not item['warmup']]
            report['summary'] = {}
            for key in ('calibration', 'teaching', 'pick', 'place'):
                values = [item['jobs'][key]['client_complete_s'] for item in measured]
                report['summary'][key + '_client_complete_s'] = {'values': values, 'median': statistics.median(values),
                                                                  'min': min(values), 'max': max(values)}
            values = [item['elapsed_s'] for item in measured]
            report['summary']['iteration_s'] = {'values': values, 'median': statistics.median(values), 'min': min(values), 'max': max(values)}
            report['outcome'] = 'passed'
        except Exception as exc:
            report['outcome'] = 'failed'
            report['error'] = str(exc)
            save()
            raise
        save()
    print(f"PASS: {args.repeats} measured simulated workflows; evidence: {args.output}")


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        print(f'benchmark: {exc}', file=sys.stderr)
        sys.exit(1)
