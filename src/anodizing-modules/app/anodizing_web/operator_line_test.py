"""Server-owned single-command tests for the in-process line simulator."""
from copy import deepcopy
import csv
from datetime import datetime
import json
import os
from pathlib import Path
from secrets import token_urlsafe
from threading import Event, RLock

from .jobs import ID, atomic_json
from .operator_line import IDENT, LIMIT_MS, Simulator, count, local_json

SESSION_S = 1800
APPROVAL_S = 30
LEASE_S = 5
RECOVERY_S = 8
ALARM = ('Restore unconfirmed. Check the command and line state. '
         'Use the line’s physical e-stop if needed. Further tests are blocked.')
BUSY = 'A line test owns the service. Wait for confirmed restore.'
TERMINAL = {'finished', 'refused'}
DISPOSITIONS = {
    'dispatching': ('Pending',), 'observing': ('Pending',), 'clearing': ('Pending',),
    'finished': ('Pass', 'Fail', 'Observed'), 'refused': ('Refused',),
    'uncertain': ('Restore unconfirmed', 'Refused'),
}


def binary(value):
    return type(value) is int and value in (0, 1)


def accepted_receipt(receipt, command, value):
    return (isinstance(receipt, dict) and receipt.get('written') is True
            and receipt.get('tag_name') == command['tag_name']
            and receipt.get('device_address') == command['device_address']
            and binary(receipt.get('value')) and receipt['value'] == value)


def simulator_cases(fixture):
    cases = fixture.get('test_cases', [])
    if not isinstance(cases, list) or len(cases) > 100:
        raise ValueError('Invalid simulator test cases.')
    result = {}
    for case in cases:
        if not isinstance(case, dict) or set(case) - {
            'id', 'command', 'feedback', 'baseline', 'expected', 'clear', 'seconds',
            'basis', 'provisional', 'guidance'
        }:
            raise ValueError('Invalid simulator test case.')
        ident, command, feedback = (case.get(key) for key in ('id', 'command', 'feedback'))
        if (any(not isinstance(name, str) or not IDENT.fullmatch(name) for name in (ident, command, feedback))
                or command == feedback or {'Remote', 'Local'}.intersection((command, feedback))
                or ident in result or not binary(case.get('baseline')) or not binary(case.get('expected'))
                or case['baseline'] == case['expected'] or not binary(case.get('clear', 0))
                or type(case.get('seconds')) is not int or not 1 <= case['seconds'] <= 15
                or not isinstance(case.get('basis'), str) or not 0 < len(case['basis']) <= 500
                or type(case.get('provisional', False)) is not bool
                or not isinstance(case.get('guidance', ''), str) or len(case.get('guidance', '')) > 1000):
            raise ValueError('Invalid simulator test case.')
        result[ident] = case
    return result


def stamped(value):
    return datetime.fromisoformat(value).utcoffset() is not None


def valid_sample(sample, case, command=None, feedback=None):
    values = sample['values']
    return (count(sample['id']) and all(stamped(sample[key]) for key in ('source_at', 'corrected_source_at', 'read_at'))
            and all(binary(values[name]) for name in ('Remote', 'Local', case['command'], case['feedback']))
            and values['Remote'] == 1 and values['Local'] == 0
            and (command is None or values[case['command']] == command)
            and (feedback is None or values[case['feedback']] == feedback))


def valid_attempt(attempt, run, value):
    return (type(attempt['ordered']) is bool and type(attempt['accepted']) is bool and stamped(attempt['at'])
            and valid_sample(attempt['baseline'], run['case'])
            and attempt['accepted'] == accepted_receipt(attempt['receipt'], run['metadata'][2], value))


def validate_saved_run(run, ident):
    state, case = run['state'], run['case']
    if (not ID.fullmatch(ident) or run['id'] != ident or run['source'] != 'Simulation'
            or not isinstance(state, str) or state not in DISPOSITIONS or run['api'] not in DISPOSITIONS[state]
            or not isinstance(run['reason'], str) or not stamped(run['at'])):
        raise ValueError('Malformed line test.')
    simulator_cases({'test_cases': [case]})
    json.dumps(run, allow_nan=False)
    if state in TERMINAL | {'uncertain'} and not stamped(run['finished_at']):
        raise ValueError('Malformed finish time.')
    judgments = run.get('judgments', [])
    if not isinstance(judgments, list) or len(judgments) > 100:
        raise ValueError('Malformed judgments.')
    for judgment in judgments + ([run['human']] if 'human' in run else []):
        if (judgment['result'] not in ('Pass', 'Fail', 'Not checked') or not isinstance(judgment['notes'], str)
                or len(judgment['notes']) > 2000 or not stamped(judgment['at'])):
            raise ValueError('Malformed judgment.')
    zeroes = run.get('zero_attempts', [])
    if not isinstance(zeroes, list) or not all(valid_attempt(attempt, run, 0) for attempt in zeroes):
        raise ValueError('Malformed restore attempts.')
    if 'dispatches' in run and (type(run['dispatches']) is not int or run['dispatches'] != ('one' in run) + len(zeroes)):
        raise ValueError('Malformed dispatch count.')
    if 'baseline' in run and not valid_sample(run['baseline'], case, 0, case['baseline']):
        raise ValueError('Malformed baseline.')
    if 'one' in run and (not valid_attempt(run['one'], run, 1) or run['one']['baseline'] != run['baseline']):
        raise ValueError('Malformed command attempt.')
    if 'observation' in run:
        observed = run['observation']
        if (not run['one']['accepted'] or not valid_sample(observed, case, 1, case['expected'])
                or not LineTest.later(observed, run['one'])):
            raise ValueError('Malformed feedback observation.')
    if 'zero_readback' in run:
        restored = run['zero_readback']
        if (not zeroes or not zeroes[-1]['accepted'] or not valid_sample(restored, case, 0, case.get('clear'))
                or not LineTest.later(restored, zeroes[-1])):
            raise ValueError('Malformed restore readback.')
    if state == 'refused' and ('one' in run or zeroes):
        raise ValueError('Refused test contains a dispatch.')
    if state == 'finished':
        api = ('Observed' if case.get('provisional') else 'Pass') if 'observation' in run else 'Fail'
        if (run['api'] != api or 'zero_readback' not in run or not run['one']['ordered']
                or not all(attempt['ordered'] for attempt in zeroes)):
            raise ValueError('Finished test has incomplete evidence.')


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def private_directory(path):
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if path.is_symlink() or not path.is_dir() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('Line test records require an owner-only directory.')


class LineTest:
    def __init__(self, root, line, owner, enabled=False, record_event=None):
        self.root = Path(root)
        self.line, self.owner = line, owner
        self.enabled = enabled and line.mode == 'simulation' and type(line.client) is Simulator
        self.cases = simulator_cases(line.client.fixture) if self.enabled else {}
        self.record_event = record_event
        self.directory = self.root / 'line-tests' / 'simulation'
        self.lock, self.wake = RLock(), Event()
        self.session = self.approval = self.active = None
        self.session_until = 0
        self.history, self.unresolved = {}, []
        self.closing = self.restore = self.storage_fault = False
        self.alarm = None
        try:
            if self.enabled or self.directory.exists() or self.directory.is_symlink():
                private_directory(self.directory.parent)
                private_directory(self.directory)
                sync_directory(self.root)
                sync_directory(self.directory.parent)
                for path in sorted(self.directory.glob('*.json'), key=lambda file: file.stat(follow_symlinks=False).st_mtime_ns):
                    try:
                        if path.is_symlink():
                            raise ValueError('Line test records must not be symbolic links.')
                        run = local_json(self.directory, path.name)
                        validate_saved_run(run, path.stem)
                    except (ValueError, OSError, RecursionError, TypeError, KeyError, IndexError):
                        self.alarm = ALARM
                        self.unresolved.append(dict(id=path.stem))
                        continue
                    if run.get('state') not in TERMINAL:
                        self.alarm = ALARM
                        self.unresolved.append(dict(id=path.stem, command=run['case'].get('command')))
                    self.history[run['id']] = run
        except (ValueError, OSError, RecursionError):
            self.alarm = ALARM
        self.owner.line_test_fence = bool(self.alarm)

    def status(self, *, summary=False):
        with self.lock:
            status = dict(enabled=self.enabled, alarm=self.alarm, active=deepcopy(self.active),
                          reason=self.reason())
            if self.history:
                latest = next(reversed(self.history.values()))
                status['latest'] = {key: latest[key] for key in ('id', 'state', 'api', 'reason')}
            if not summary:
                status.update(cases=list(self.cases.values()), unresolved=self.unresolved,
                              history=deepcopy(list(reversed(self.history.values()))[:100]))
            return status

    def export(self, ident, format, stream):
        with self.lock:
            if ident not in self.history:
                raise ValueError('Select a saved line test.')
            run = self.history[ident]
        if format == 'json':
            json.dump(run, stream, ensure_ascii=False, indent=2, allow_nan=False)
        elif format == 'csv':
            writer = csv.writer(stream)
            writer.writerow(['Transport', 'Test', 'Command', 'API disposition', 'Human result', 'Evidence', 'Value'])
            for key, value in run.items():
                writer.writerow(['Simulation', ident, run['case']['command'], run['api'],
                                 run.get('human', {}).get('result', 'Not checked'), key,
                                 json.dumps(value, ensure_ascii=False, allow_nan=False)])
        elif format == 'summary':
            human = run.get('human', {})
            stream.write('# Line test · Simulation\n\n| Test | API disposition | Human result |\n| --- | --- | --- |\n'
                         f"| {ident} | {run['api']} | {human.get('result', 'Not checked')} |\n\n"
                         f"Command: {run['case']['command']}\n\nFeedback: {run['case']['feedback']}\n\n"
                         f"{run['reason']}\n\n")
            if human.get('notes'):
                stream.write('Human notes\n\n' + human['notes'] + '\n')
        else:
            raise ValueError('Select a line test export format.')

    def reason(self):
        if self.alarm:
            return self.alarm
        if not self.enabled:
            return 'Simulator writes are disabled. Check the simulator setup.'
        if self.closing or self.owner.closing:
            return 'Service is closing. Reopen the page after restart.'
        if self.active:
            return BUSY
        if not self.cases:
            return 'No reviewed simulator test cases are configured.'
        if self.owner.active or self.owner.mcu_pending or self.owner.execution_uncertain or self.owner.storage_error:
            return 'Another operation owns the service or needs reconciliation. Check its status.'
        if self.owner.agv is not None and self.owner.agv.transport is not None:
            return 'Disconnect the AGV transport before starting a line test.'
        return None

    def sample(self, case, identity=None):
        with self.line.lock:
            tags, sample = self.line.tags, self.line.sample
            names = ('Remote', 'Local', case['command'], case['feedback'])
            metadata = [tags.get(name) for name in names]
            if any(tag is None or tag['data_type'] != 'Bool' or tag['multiplier'] != 1
                   for tag in metadata):
                raise ValueError('Test metadata changed. Review the command and feedback catalog.')
            for tag, name in zip(metadata, names):
                address = tag['device_address'].upper()
                if (sum(other['device_address'].upper() == address for other in tags.values()) != 1
                        or self.line.expected is not None and tag != self.line.expected.get(name)):
                    raise ValueError('Mapping is ambiguous or changed. Review the catalog and local baseline.')
            command = metadata[2]
            if ([tag['read_write'] for tag in metadata] != ['R', 'R', 'W', 'R']
                    or command['minimum'] is not None and command['minimum'] > 0
                    or command['maximum'] is not None and command['maximum'] < 1
                    or identity is not None and metadata != identity):
                raise ValueError('Test permissions or limits changed. Review the catalog.')
            if (sample is None or self.line.reason or sample['partial']
                    or self.line.clock() - sample['monotonic_at'] >= LIMIT_MS / 1000):
                raise ValueError('A complete fresh PLC row is required. Check the simulator read feed.')
            values = sample['values']
            if values['Remote'] != 1 or values['Local'] != 0:
                raise ValueError('A test needs Remote = 1 and Local = 0. Check the reported mode.')
            return {**{key: sample[key] for key in ('id', 'source_at', 'corrected_source_at', 'read_at')},
                    'values': {name: values[name] for name in names}}, metadata

    def baseline(self, case, identity=None):
        sample, metadata = self.sample(case, identity)
        if sample['values'][case['command']] != 0 or sample['values'][case['feedback']] != case['baseline']:
            raise ValueError('Command or feedback baseline is incorrect. Check the configured test case.')
        return sample, metadata

    def persist(self, run):
        try:
            atomic_json(self.directory / (run['id'] + '.json'), run)
            sync_directory(self.directory)
            return True
        except (ValueError, OSError):
            self.storage_fault = True
            self.alarm = ALARM
            self.owner.line_test_fence = True
            return False

    def evidence(self, event, **fields):
        if self.record_event is not None:
            self.record_event('line-test', event, request_id=self.active['id'], test_id=self.active['id'],
                              transport='Simulation', **fields)

    def action(self, body):
        action, ident = body.get('action'), body.get('request_id')
        fields = {
            'arm': set(), 'prepare': {'session', 'case', 'request_id'},
            'start': {'session', 'request_id', 'approval'}, 'disarm': {'session'}, 'heartbeat': {'session'},
            'stop': {'request_id'}, 'judge': {'request_id', 'result', 'notes'},
        }
        if (not isinstance(action, str) or action not in fields or set(body) != {'action'} | fields[action]
                or 'request_id' in fields[action] and (not isinstance(ident, str) or not ID.fullmatch(ident))):
            raise ValueError('Choose a valid line test action.')
        with self.lock:
            now = self.line.clock()
            if action == 'stop':
                if self.active and self.active['id'] == ident:
                    self.restore = True
                    self.wake.set()
                    return {'id': ident}
                if ident in self.history:
                    return {'id': ident}
                raise ValueError('This line test is no longer available. Refresh its status.')
            if action == 'judge':
                return self.judge(ident, body['result'], body['notes'])
            if action == 'heartbeat':
                if self.active and self.session is not None and self.session == body['session'] and now < self.lease_until:
                    self.lease_until = now + LEASE_S
                return {}
            if action == 'disarm':
                if self.session is not None and self.session == body['session']:
                    self.session, self.approval = None, None
                    self.restore = bool(self.active)
                    self.wake.set()
                return {}
            if action == 'start' and self.active and ident == self.active['id']:
                if self.session is not None and self.session == body['session'] and self.start_token == body['approval']:
                    return {'id': ident}
                raise ValueError('Approval does not match this test. Review a new test.')
            if reason := self.reason():
                raise ValueError(reason)
            if action == 'arm':
                self.session, self.session_until = token_urlsafe(24), now + SESSION_S
                self.approval = None
                return dict(session=self.session, seconds=SESSION_S)
            if self.session is None or self.session != body['session'] or now >= self.session_until:
                raise ValueError('Arm a new simulator write session.')
            if ident in self.history:
                raise ValueError('This test ID was already used. Review a new test.')
            if action == 'prepare':
                name = body['case']
                case = self.cases.get(name) if isinstance(name, str) else None
                if case is None:
                    raise ValueError('Choose a reviewed simulator test case.')
                baseline, metadata = self.baseline(case)
                approval = token_urlsafe(24)
                self.approval = (approval, now + APPROVAL_S, ident, case, metadata)
                return dict(approval=approval, baseline=baseline, metadata=metadata, seconds=APPROVAL_S)
            approval = self.approval
            if not approval or approval[0] != body['approval'] or now >= approval[1] or ident != approval[2]:
                raise ValueError('Test approval expired or changed. Review a new test.')
            with self.owner.lock:
                if reason := self.reason():
                    raise ValueError(reason)
                refresh = self.line.queue_refresh()
                self.owner.line_test_fence = True
            self.approval, self.start_token = None, approval[0]
            self.restore = False
            self.active = dict(id=ident, source='Simulation', state='checking', api='Pending',
                               case=approval[3], metadata=approval[4], at=self.line.wall().isoformat(),
                               reason='Checking the catalog and baseline.')
            self.refresh_id, self.approval_until = refresh, approval[1]
            self.lease_until = now + LEASE_S
            return {'id': ident}

    def judge(self, ident, result, notes):
        if (ident not in self.history or result not in ('Pass', 'Fail', 'Not checked')
                or not isinstance(notes, str) or len(notes) > 2000):
            raise ValueError('Select a saved test and enter up to 2,000 characters of notes.')
        human = dict(result=result, notes=notes, at=self.line.wall().isoformat())
        old = self.history[ident]
        if len(old.get('judgments', [])) >= 100:
            raise ValueError('This record has reached its judgment limit. Export the saved evidence.')
        run = dict(old, human=human, judgments=[*old.get('judgments', []), human])
        if not self.persist(run):
            raise ValueError('Could not save the judgment. Check the line test data directory.')
        self.history[ident] = run
        return run

    def finish(self, state, api, reason):
        run = self.active
        run.update(state=state, api=api, reason=reason, finished_at=self.line.wall().isoformat(),
                   dispatches=int('one' in run) + len(run.get('zero_attempts', [])))
        if not self.persist(run):
            run.update(state='uncertain', api='Restore unconfirmed', reason=ALARM)
            state = 'uncertain'
        if state == 'uncertain':
            self.alarm = ALARM
        else:
            with self.owner.lock:
                self.owner.line_test_fence = False
            self.alarm = None
        self.evidence(state, api=run['api'], reason=run['reason'])
        self.history[run['id']], self.active = run, None

    def write(self, value, baseline):
        run, command = self.active, self.active['metadata'][2]
        try:
            receipt, ordered = self.line.client.write(run['case'], value)
        except (OSError, ValueError):
            receipt, ordered = None, False
        attempt = dict(at=self.line.wall().isoformat(), accepted=accepted_receipt(receipt, command, value),
                       ordered=ordered is True, receipt=receipt, baseline=baseline)
        self.evidence('dispatch', value=value, **attempt)
        return attempt

    @staticmethod
    def later(sample, attempt):
        return (sample['id'] > attempt['baseline']['id']
                and datetime.fromisoformat(sample['corrected_source_at']) >
                max(datetime.fromisoformat(attempt['at']),
                    datetime.fromisoformat(attempt['baseline']['corrected_source_at'])))

    def tick(self):
        with self.lock:
            if not self.active:
                return
            now = self.line.clock()
            run, case = self.active, self.active['case']
            stop = self.restore or self.closing or now >= self.session_until or now >= self.lease_until
            if run['state'] == 'checking':
                if stop or self.owner.closing or now >= self.approval_until:
                    self.finish('refused', 'Refused', 'Test stopped or approval expired before dispatch. Review a new test.')
                    return
                refresh = self.line.refresh
                if refresh['id'] < self.refresh_id or refresh['outcome'] == 'pending':
                    return
                try:
                    if refresh['outcome'] != 'done':
                        raise ValueError('Catalog refresh failed. Check the simulator setup.')
                    if self.owner.active or self.owner.mcu_pending or self.owner.storage_error or self.owner.execution_uncertain or (
                        self.owner.agv is not None and self.owner.agv.transport is not None
                    ):
                        raise ValueError('Another operation owns the service. Wait before starting a test.')
                    baseline, _ = self.baseline(case, run['metadata'])
                except ValueError as exc:
                    self.finish('refused', 'Refused', str(exc))
                    return
                run.update(state='dispatching', baseline=baseline, reason='Command dispatch was reserved.')
                if not self.persist(run):
                    self.finish('uncertain', 'Refused', 'Intent storage failed. No command was sent.')
                    return
                try:
                    self.baseline(case, run['metadata'])
                    now = self.line.clock()
                    if now >= self.lease_until:
                        raise ValueError('Page lease expired before dispatch. Review a new test.')
                    if self.restore or self.closing or self.owner.closing or now >= min(self.approval_until, self.session_until):
                        raise ValueError('Test stopped or approval expired before dispatch. Review a new test.')
                except ValueError as exc:
                    self.finish('refused', 'Refused', str(exc))
                    return
                run['one'] = self.write(1, baseline)
                self.observe_until = self.line.clock() + case['seconds']
                run.update(state='observing', deadline_s=case['seconds'], reason='Waiting for later feedback.')
                if self.persist(run) and run['one']['accepted'] and run['one']['ordered']:
                    return
                stop = True

            sample, issue = None, None
            try:
                sample, _ = self.sample(case, run['metadata'])
            except ValueError as exc:
                issue = str(exc)
            if run['state'] == 'observing':
                attempt = run['one']
                matched = (not stop and now < self.observe_until and sample is not None and
                           self.later(sample, attempt) and sample['values'][case['command']] == 1 and
                           sample['values'][case['feedback']] == case['expected'])
                if matched:
                    run['observation'] = sample
                    self.evidence('feedback', sample=sample)
                if not (matched or stop or issue or now >= self.observe_until):
                    run['deadline_s'] = max(0, self.observe_until - now)
                    return
                run.update(state='clearing', reason='Restoring the same command to zero.', cleanup_reason='Feedback matched' if matched else
                           'Clear requested' if self.restore else 'Service closing' if self.closing else
                           'Write session expired' if now >= self.session_until else
                           'Page lease expired' if now >= self.lease_until else
                           'Receipt or storage uncertain' if stop else issue or 'Observation expired')
                self.recover_until = now + RECOVERY_S
                self.check_clear_catalog_at = now
                self.persist(run)
            self.clear(run, case, sample, issue, now)

    def clear(self, run, case, sample, issue, now):
        last = (run.get('zero_attempts') or [None])[-1]
        run['deadline_s'] = max(0, self.recover_until - now)
        if now >= self.recover_until:
            self.finish('uncertain', 'Restore unconfirmed', ALARM)
            return
        if last and last['accepted'] and sample and self.later(sample, last):
            values = sample['values']
            if values[case['command']] == 0 and ('clear' not in case or values[case['feedback']] == case['clear']):
                run['zero_readback'] = sample
                if self.storage_fault or not run['one']['ordered'] or not all(a['ordered'] for a in run['zero_attempts']):
                    self.finish('uncertain', 'Restore unconfirmed', ALARM)
                    return
                observed = 'observation' in run
                api = ('Observed' if case.get('provisional') else 'Pass') if observed else 'Fail'
                self.finish('finished', api, 'Command zero confirmed. ' + run['cleanup_reason'] + '.')
                return
        if (last is None or not last['accepted']) and now >= self.check_clear_catalog_at:
            self.check_clear_catalog_at = now + 1
            if not self.line.refresh_catalog():
                self.alarm = ALARM
                return
            try:
                sample, _ = self.sample(case, run['metadata'])
            except ValueError:
                self.alarm = ALARM
                return
        else:
            if issue or self.storage_fault or not run['one']['ordered']:
                self.alarm = ALARM
            return
        if last is None or self.later(sample, last):
            run['zero_baseline'] = sample
            self.persist(run)
            try:
                if self.line.clock() >= self.recover_until:
                    raise ValueError(ALARM)
                self.sample(case, run['metadata'])
            except ValueError:
                self.alarm = ALARM
                return
            last = self.write(0, sample)
            run.setdefault('zero_attempts', []).append(last)
            self.persist(run)
        if issue or self.storage_fault or not run['one']['ordered'] or last and not last['accepted']:
            self.alarm = ALARM
