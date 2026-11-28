"""Allowlisted wired AGV access under the IPC gateway's existing authority."""
from collections import deque
from copy import deepcopy
from time import monotonic, sleep, time
from uuid import uuid4

from . import agv
from .jobs import ID
from .recipe_agv import stopped

MOTION = {'call_station', 'stop', 'pause'}
BLOCKED = {
    'forward': 'Forward has no bounded distance on the wired protocol.',
    'reverse': 'Reverse has no bounded distance on the wired protocol.',
    'resume': 'Resume reuses an earlier target without a bounded distance.',
    'auto_start': 'Raw access cannot enable automatic restart.',
    'obstacle_sensing': 'Raw access cannot weaken obstacle sensing.',
    'bumper': 'Raw access cannot weaken bumper protection.',
    'e_stop': 'Raw access cannot change emergency-stop protection.',
    'speed_limit': 'Raw access cannot change speed limits.',
}
for _command in agv.COMMANDS:
    if _command.startswith(('rotate_', 'attachment_')):
        BLOCKED[_command] = 'This optional attachment or rotation is unverified on this vehicle.'

SPEED_CAP_M_H = 500
# Vendor protocol p. 2: 90 position units per 0.48 m. Abort at 3 m sampled travel.
DISTANCE_CAP_UNITS = 562


class RawAccess:
    def __init__(self, executor):
        self.executor = executor
        self.recent = deque(maxlen=10)
        self.snapshots = {}
        self.unreviewed = {}
        self.pending_motion = set()
        self.refresh = False
        self.owner_grant = None

    def load(self, entry):
        if 'raw_snapshot' in entry:
            snapshot = entry['raw_snapshot']
            self.snapshots[snapshot['id']] = snapshot
            self.unreviewed[snapshot['id']] = snapshot
            self.refresh = True
        if 'raw_activity' in entry:
            row = entry['raw_activity']
            if row['outcome'] == 'pending':
                self.pending_motion.add(row['id'])
            else:
                self.recent.append(row)
            if row['outcome'] != 'refused':
                self.refresh = True
            if 'snapshot' in row:
                self.snapshots[row['id']]['outcome'] = row['outcome']
            if row['outcome'] == 'verified':
                self.pending_motion.discard(row['id'])
                if row.get('undo_of'):
                    self.snapshots[row['undo_of']]['undone'] = True
        for ident in entry.get('raw_reviewed', []):
            self.unreviewed.pop(ident, None)
            self.pending_motion.discard(ident)

    def outstanding(self):
        return list(self.unreviewed) + sorted(self.pending_motion)

    def record(self, entry):
        executor = self.executor
        with executor.owner.lock:
            executor._append(entry)
            self.load(entry)

    def end_session(self):
        self.owner_grant = None
        if not self.outstanding():
            return
        executor = self.executor
        changes = ', '.join(f"{row['before']} to {row['after']} m/h ({row['id']})"
                            for row in self.unreviewed.values())
        reason = 'Raw motion outcome is uncertain. Verify the AGV hold.' if self.pending_motion else ''
        if changes:
            reason = (f'{reason} Raw speed writes or attempts: {changes}. Verify the current setpoint. '
                      'To restore a prior value after review, use Raw undo with its snapshot ID.').strip()
        if executor.check:
            if reason not in executor.check['reason']:
                executor.check = dict(executor.check, reason=f"{executor.check['reason']} {reason}")
        else:
            executor._hold(reason, facts=dict(idle=True, manual=True))
        executor.pending_check_write = True

    def before_normal_job(self):
        if self.outstanding():
            raise ValueError('Release the raw settings session and complete Operator check before submitting.')
        if self.refresh:
            devices = self.executor.devices
            previous = devices.status().get('status_monotonic')
            devices.read()
            current = devices.status()
            stamp = current.get('status_monotonic')
            if (not current['connected'] or current.get('stale') or stamp is None
                    or previous is not None and stamp <= previous
                    or not 0 <= current['status_age_s'] < agv.RECIPE_STALE_S):
                raise ValueError('A fresh AGV status reply is required after raw access.')
            self.refresh = False

    def request(self, body, principal, *, undo=False):
        executor = self.executor
        if undo:
            if set(body) != {'lease_id', 'snapshot_id'} or not (
                    isinstance(body['snapshot_id'], str) and ID.fullmatch(body['snapshot_id'])):
                raise ValueError('Provide the current lease and a speed snapshot ID.')
            name, value = 'set_speed', None
        else:
            if not {'command', 'args'} <= set(body) <= {'lease_id', 'command', 'args'}:
                raise ValueError('Provide a raw command, args and, for writes, the current lease.')
            name, args = body['command'], body['args']
            if not isinstance(name, str) or name not in agv.READS | MOTION | {'set_speed'}:
                raise ValueError(BLOCKED.get(name, 'Command is outside the wired allowlist. '
                                            'Unknown registers and safety settings are blocked.')
                                 if isinstance(name, str) else 'Choose an allowlisted command name.')
            if not isinstance(args, dict) or set(args) != ({'value'} if name in agv.ARGUMENTS else set()):
                raise ValueError('The raw command arguments do not match the allowlist.')
            value = args.get('value')
            agv.command_frame(name, value, b'')

        if 'lease_id' in body and (not isinstance(body['lease_id'], str) or not ID.fullmatch(body['lease_id'])):
            raise ValueError('Provide a valid module lease ID.')
        row = dict(id=uuid4().hex, utc=agv.timestamp(), command='undo' if undo else name,
                   client_name=principal.name, token_id=principal.token_id, wire=[])
        with executor.admission:
            executor.gateway.revalidate(principal)
            executor._scan_accepted()
            executor._require_intact_evidence()
            executor.gateway.revalidate(principal)
            try:
                return self._dispatch(body, principal, row, name, value, undo)
            except (OSError, ValueError, RuntimeError) as exc:
                if row['wire'] or 'snapshot' in row or row['id'] in self.pending_motion:
                    row.update(outcome='unverified', error=str(exc))
                    if row['id'] in self.pending_motion or 'snapshot' in row:
                        with executor.guard:
                            self.end_session()
                else:
                    row.update(outcome='refused', error=str(exc), request_digest=executor._digest(body))
                raise
            finally:
                if 'outcome' in row:
                    self.record(dict(raw_activity=row))

    def _dispatch(self, body, principal, row, name, value, undo):
        executor, devices = self.executor, self.executor.devices
        settings, motion = name == 'set_speed', name in MOTION

        def trace(direction, raw):
            fields = dict(utc=agv.timestamp(), client_name=principal.name, token_id=principal.token_id,
                          raw_id=row['id'])
            row['wire'].append(dict(fields, direction=direction, hex=raw.hex()))
            return fields

        with executor.guard:
            executor._expire()
            ownership = executor.gateway.ownership
            if ownership.owner in ('main-app-loop', 'calibration'):
                raise ValueError('Raw access is unavailable while the main app or Calibration owns control.')
            if executor.active or executor.owner.active or executor.open_air or executor._retained_simulated_basket():
                raise ValueError('Raw access is unavailable during an active job or recipe.')
            if executor.recovery:
                raise ValueError('Complete Operator check before raw access.')
            lease = executor.gateway._module_lease(body, principal) if settings or motion else ownership.lease
            lease_id = lease.id if lease is not None else None
            generation = executor.generation
            if motion:
                executor._check_module_track()
                executor._check_module_attestation()
                executor.gateway.check_armed(generation)
            if settings and (executor.armed or executor.disarm_pending or executor.arming):
                raise ValueError('Disarm before writing raw settings.')
            if lease is not None and lease.principal.token_id == principal.token_id:
                self.owner_grant = principal

        def frame_authority():
            return executor.gateway.raw_frame_authority(principal)

        def authorize(*, dispatch=False, check_grant=True):
            if check_grant:
                executor.gateway.revalidate(principal)
            if executor.gateway.keys.raw_expired(principal):
                raise ValueError('The raw scope has expired.')
            if (executor.generation != generation or executor._stop_pending() or executor.stop_workers
                    or executor.stop_result and not executor.stop_result['confirmed'] or executor.recovery):
                raise ValueError('Stop or Operator check is pending. Review current status.')
            if (settings or motion) and (ownership.lease is not lease or lease.id != lease_id
                                         or ownership.owner != principal.name or executor.clock() >= lease.deadline):
                raise ValueError('The module lease expired before raw dispatch.')
            if motion:
                executor._check_module_track()
            if settings and (executor.armed or executor.disarm_pending or executor.arming):
                raise ValueError('Disarm before writing raw settings.')
            if motion and executor.manual_travel_until is not None and time() >= executor.manual_travel_until:
                raise ValueError('The manual travel window expired during raw access.')
            if dispatch:
                executor._check_module_attestation()
                executor.gateway.check_armed(generation)

        authorize(dispatch=motion)
        if motion and not devices.simulated and not (
                executor.manual_travel_until is not None and (executor.isolated_track or devices.manual_travel_qualified)):
            raise ValueError('Raw motion requires a supervised window and qualified local interlocks.')
        self.refresh = True
        with devices.raw_audit(trace):
            if settings:
                result = self._settings(row, body, value, authorize, undo, frame_authority)
            elif motion:
                result = self._motion(row, name, value, authorize, principal, frame_authority)
            else:
                result = devices.raw_exchange(name, None, authorize, frame_authority=frame_authority)
        row.update(outcome='verified', reply_hex=result['reply_hex'], decoded=result['decoded'])
        snapshot = row.get('snapshot')
        if (motion or settings) and 'read' not in principal.scopes:
            receipt = dict(id=row['id'], verified=True)
            if snapshot is not None:
                receipt['snapshot'] = {'id': snapshot['id']}
            return receipt
        return dict(result, **({'snapshot': deepcopy(snapshot)} if snapshot is not None else {}))

    def _settings(self, row, body, value, authorize, undo, frame_authority):
        devices = self.executor.devices
        state = devices.status()
        report = state['status'] or {}
        age = state.get('status_age_s')
        if (not state['connected'] or state.get('stale') or age is None or not 0 <= age < agv.RECIPE_STALE_S
                or state['armed'] or not stopped(report) or type(report.get('vehicle_id')) is not int):
            raise ValueError('Raw settings require fresh, disarmed and stopped feedback from the vehicle.')
        vehicle = report['vehicle_id']
        prior = devices.raw_exchange('read_speed', None, authorize, frame_authority=frame_authority)
        before = prior['decoded']['read_speed_m_h']
        agv.command_frame('set_speed', before, b'')
        if undo:
            snapshots = (item for item in reversed(self.snapshots.values())
                         if item['vehicle_id'] == vehicle and not item.get('undo_of') and not item.get('undone'))
            original = next(snapshots, None)
            if (original is None or original['id'] != body['snapshot_id']
                    or original['token_id'] != row['token_id'] or original['after'] != before):
                raise ValueError('Undo needs the latest matching snapshot, its original key and unchanged read-back.')
            row['undo_of'] = original['id']
            value = original['before']
        elif value > before:
            raise ValueError('Raw writes cannot increase the current speed setpoint.')
        if value == before:
            return prior
        snapshot = dict(id=row['id'], before=before, after=value, vehicle_id=vehicle,
                        token_id=row['token_id'], client_name=row['client_name'], utc=row['utc'])
        if undo:
            snapshot['undo_of'] = row['undo_of']
        self.record(dict(raw_snapshot=snapshot))
        row['snapshot'] = dict(snapshot)
        devices.raw_exchange('set_speed', value, authorize, expected=(vehicle, before),
                             frame_authority=frame_authority)
        result = devices.raw_exchange('read_speed', None, authorize, frame_authority=frame_authority)
        if result['decoded']['read_speed_m_h'] != value:
            raise ValueError('The raw speed read-back did not match. Check the saved snapshot and AGV.')
        return result

    def _motion(self, row, name, value, authorize, principal, frame_authority):
        from .executor import check_manual_feedback

        executor, devices = self.executor, self.executor.devices
        state = devices.status()
        check_manual_feedback(state, executor.manual_travel_min_battery, value, above_floor=executor.isolated_track)
        report = state['status']
        if name == 'call_station':
            current, origin = report.get('current_station'), report.get('position_units')
            if (type(current) is not int or abs(current - value) != 1 or type(origin) is not int
                    or report.get('station_arrival') is not True or not stopped(report)):
                raise ValueError('Raw station calls need a verified stopped station, position and adjacent target.')

            def monitor(sample, *, dispatch=False):
                # The final frame fence checks the registry. The travel watcher must never wait on it.
                authorize(dispatch=dispatch, check_grant=False)
                if dispatch and (sample.get('current_station') != current or not stopped(sample)):
                    raise ValueError('The starting station changed before raw departure.')
                position = sample.get('position_units')
                actual = sample.get('actual_speed_m_h')
                if (sample.get('set_speed_m_h') != SPEED_CAP_M_H or type(actual) is not int
                        or abs(actual) > SPEED_CAP_M_H):
                    raise ValueError('Raw travel exceeds the 500 m/h speed cap or feedback is missing.')
                if type(position) is not int or abs(position - origin) >= DISTANCE_CAP_UNITS:
                    raise ValueError('Raw travel reached the 3 m sampled distance cap or position evidence was lost.')

            monitor(report)
            self.record(dict(raw_activity=dict(row, outcome='pending', wire=[])))
            return devices.raw_motion(value, authorize, monitor, frame_authority=frame_authority)
        if report['mode'] not in (0, 2) or name == 'pause' and report['mode'] != 2:
            raise ValueError('Raw Pause requires station-call mode. Stop needs a verified drive mode.')
        if not devices.simulated:
            devices._travel_interlocks()
        with frame_authority():
            if executor.isolated_track:
                check_manual_feedback(devices.status(), executor.manual_travel_min_battery, above_floor=True)
            with executor.stop_ingress_lock:
                authorize(dispatch=True, check_grant=False)
                receipt = executor.stop(source=principal.token_id)
        deadline = monotonic() + 6
        while monotonic() < deadline:
            with executor.stop_ingress_lock:
                ticket = executor.stop_tickets.get((principal.token_id, receipt['stop_id']))
                if ticket is None:
                    break
                confirmed = ticket[1]['confirmed']
            if confirmed:
                def after_halt(*, check_grant=True):
                    if check_grant:
                        executor.gateway.revalidate(principal)
                    if (executor.gateway.keys.raw_expired(principal) or executor.generation != receipt['generation']
                            or executor._stop_pending() or executor.recovery):
                        raise ValueError('The raw Stop reply was superseded.')

                return devices.raw_exchange('query_status', None, after_halt, frame_authority=frame_authority)
            sleep(.05)
        raise ValueError('Raw Stop is unconfirmed. Check the AGV hold.')
