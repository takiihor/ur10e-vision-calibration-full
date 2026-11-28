"""Keyed IPC job API and the authority for its single current owner."""
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass
from typing import NotRequired, TypedDict
from uuid import uuid4

from .jobs import ID
from .module_tokens import KeyStore, ModulePrincipal, SCOPES, ServicePrincipal
from .raw_agv import RawAccess

MODULE_LEASE_S = 10
OPERATOR = ServicePrincipal('agv-page', 'katana', frozenset({'operator'}))
READ_OPERATIONS = {'health', 'status', 'jobs'}
MODULE_REQUEST_FIELDS = {
    'claim': set(),
    'release': {'lease_id'},
    'heartbeat': {'lease_id', 'sequence'},
    'submit': {'lease_id', 'stop_generation', 'job_id', 'type', 'version', 'params'},
}


class GatewayForbidden(ValueError):
    pass


class Job(TypedDict):
    job_id: str
    state: str
    outcome: str | None
    checkpoint: dict | None
    result: dict
    error: str | None
    client_name: NotRequired[str]
    token_id: NotRequired[str]


class JobAPI(ABC):
    @abstractmethod
    def claim(self, body=None, *, principal=OPERATOR): ...

    @abstractmethod
    def release(self, body=None, *, principal=OPERATOR): ...

    @abstractmethod
    def submit(self, body, *, principal=OPERATOR) -> Job: ...

    @abstractmethod
    def status(self, body=None, *, principal=OPERATOR): ...

    @abstractmethod
    def list(self, *, principal=OPERATOR): ...

    @abstractmethod
    def heartbeat(self, body, *, principal=OPERATOR): ...

    @abstractmethod
    def stop(self, body=None, *, principal=OPERATOR): ...


@dataclass
class ModuleLease:
    principal: ModulePrincipal
    id: str
    deadline: float
    sequence: int = -1


@dataclass
class Ownership:
    owner: str | None = None
    lease: ModuleLease | None = None
    armed: bool = False

    @property
    def armed_for(self):
        return self.owner if self.armed else None


class JobGateway(JobAPI):
    def __init__(self, executor, key_path, *, enabled):
        self.executor = executor
        self.enabled = enabled
        self.keys = KeyStore(key_path)
        self.ownership = Ownership()
        self.raw_access = RawAccess(executor)

    def configure_keys(self, credential, store=None):
        if store is not None:
            self.keys = store
        self.keys.configure_service_keys(credential)

    def authenticate(self, key, *, allow_modules):
        return self.keys.authenticate_client(key, allow_modules=allow_modules)

    def _require_scope(self, principal, scope):
        if not self.enabled:
            raise ValueError('The job gateway is unavailable.')
        if not isinstance(principal, (ModulePrincipal, ServicePrincipal)):
            raise GatewayForbidden('A valid gateway key is required.')
        if scope == 'operator' and scope not in principal.scopes:
            raise GatewayForbidden('This operation requires the operator credential. Use the AGV page.')
        if scope == 'raw' and not (isinstance(principal, ModulePrincipal) and 'raw' in principal.scopes
                                   and not self.keys.raw_expired(principal)):
            raise GatewayForbidden('This endpoint needs a current raw scope. Ask the IPC administrator for access.')
        if scope is not None and 'operator' not in principal.scopes and scope not in principal.scopes:
            raise GatewayForbidden(f'This endpoint needs the {scope} scope. Ask the IPC administrator for access.')

    def revalidate(self, principal):
        if isinstance(principal, ModulePrincipal) and principal.check_current and not self.keys.current_grant(principal):
            raise GatewayForbidden('The module grant changed. Request authorization again.')

    @contextmanager
    def raw_frame_authority(self, principal):
        if not principal.check_current:
            yield
            return
        with self.keys.hold_grant(principal) as current:
            if not current:
                raise GatewayForbidden('The module grant changed. Request authorization again.')
            yield

    def change_owner(self, owner, lease=None):
        executor = self.executor
        if self.raw_access.owner_grant is not None:
            self.raw_access.end_session()
        self.ownership.owner = owner
        self.ownership.lease = lease
        executor.armed = False
        executor.module_arm_nonce = uuid4().hex
        executor.disarm_pending = True
        # A new owner must review a new generation before any positive dispatch.
        executor.control_generation = next(executor.generations)

    def watch_heartbeat(self, now):
        lease = self.ownership.lease
        if lease is None:
            return False
        raw_grant = self.raw_access.owner_grant
        raw_ended = raw_grant is not None and self.keys.raw_expired(raw_grant)
        if now >= lease.deadline or raw_ended:
            executor = self.executor
            executor.stop()
            self.change_owner(None)
            retaining = executor.active or executor.open_air or executor._retained_simulated_basket()
            reason = ('The raw scope expired. Verify the AGV hold before arming again.' if raw_ended
                      else 'The module heartbeat expired. Verify the AGV hold before arming again.')
            executor._hold(reason,
                           facts=None if retaining else dict(idle=True, manual=True), link_loss=not raw_ended)
            executor.pending_check_write = True
        return True

    def _module_lease(self, body, principal):
        self.executor._expire()
        lease = self.ownership.lease
        if (lease is None or lease.principal.token_id != principal.token_id or lease.principal.name != principal.name
                or body.get('lease_id') != lease.id):
            raise ValueError('No current module lease matches this request. Claim ownership first.')
        return lease

    def check_owner(self, body, client_name, principal):
        if principal is not None:
            self._module_lease(body, principal)
        elif self.enabled and (client_name is None or self.ownership.owner != client_name):
            raise ValueError('Only the current owner can submit a new job. Claim ownership first.')
        else:
            self.executor._lease(body)

    def check_armed(self, generation):
        executor = self.executor
        if (executor._stop_pending() or not self.ownership.armed or type(generation) is not int
                or generation != executor.generation):
            raise ValueError('Arm explicitly and refresh the Stop generation before submitting.')

    def _transition(self, body, principal, *, releasing):
        self._require_scope(principal, 'jobs')
        external = isinstance(principal, ModulePrincipal)
        if external and not set(body) <= MODULE_REQUEST_FIELDS['release' if releasing else 'claim']:
            raise ValueError('The request contains an unsupported field.')
        executor, ownership = self.executor, self.ownership
        with executor.admission:
            self.revalidate(principal)
            with executor.guard:
                executor._expire()
                if not external:
                    executor._lease(body)
                if releasing:
                    if external:
                        self._module_lease(body, principal)
                    elif ownership.owner != principal.name:
                        raise ValueError('Only the current owner can release ownership.')
                    if executor.active or executor.owner.active:
                        raise ValueError('A job is active. Wait for it to finish or use Stop before releasing.')
                    self.change_owner(None)
                elif ownership.owner is not None:
                    if (ownership.owner != principal.name or
                            (external and ownership.lease.principal.token_id != principal.token_id)):
                        raise ValueError(f'{ownership.owner} owns the executor. Wait for it to release ownership.')
                else:
                    if executor.active or executor.owner.active:
                        raise ValueError('A job is active. Wait for it to finish before claiming ownership.')
                    lease = ModuleLease(principal, uuid4().hex, executor.clock() + MODULE_LEASE_S) if external else None
                    self.change_owner(principal.name, lease)
            executor._apply_disarm()
            if not external:
                return self.status(principal=principal)
            with executor.guard:
                lease = ownership.lease
                return dict(owner=ownership.owner, lease_id=lease.id if lease else None,
                            lease_s=MODULE_LEASE_S, boot=executor.boot, stop_generation=executor.generation,
                            armed=ownership.armed and not executor._stop_pending())

    def claim(self, body=None, *, principal=OPERATOR):
        return self._transition(body or {}, principal, releasing=False)

    def release(self, body=None, *, principal=OPERATOR):
        return self._transition(body or {}, principal, releasing=True)

    def submit(self, body, *, principal=OPERATOR) -> Job:
        self._require_scope(principal, 'jobs')
        external = isinstance(principal, ModulePrincipal)
        if external and not set(body) <= MODULE_REQUEST_FIELDS['submit']:
            raise ValueError('The request contains an unsupported field.')
        if 'operator' not in principal.scopes and 'attestation' in body:
            raise GatewayForbidden('Only the AGV page may send a manual station-call confirmation.')
        return self.executor.submit(body, client_name=principal.name, principal=principal if external else None,
                                    client_token_id=principal.token_id)

    def _client_owns_job(self, ident, principal):
        payload = (self.executor.owner.jobs.get(ident) or {}).get('executor_payload') or {}
        return payload.get('client_name') == principal.name and (
            isinstance(principal, ServicePrincipal) or payload.get('token_id') == principal.token_id)

    def records(self, principal=None, *, summaries=False):
        # The executor's durable job journal is the only store for accepted jobs.
        executor = self.executor
        rows = []
        for ident in reversed(executor.owner.jobs):
            if principal is not None and not self._client_owns_job(ident, principal):
                continue
            if summaries:
                job = executor.owner.jobs[ident]
                payload = job.get('executor_payload') or {}
                rows.append(dict(job_id=ident, state=job['state'], outcome=(job.get('result') or {}).get('outcome'),
                                 client_name=payload.get('client_name'), token_id=payload.get('token_id')))
            else:
                rows.append(executor._public_job(ident))
            if len(rows) == 10:
                break
        return rows

    def list(self, *, principal=OPERATOR):
        self._require_scope(principal, 'read')
        executor = self.executor
        with executor.owner.lock:
            executor._scan_accepted()
            return dict(jobs=self.records(None if 'operator' in principal.scopes else principal))

    def status(self, body=None, *, principal=OPERATOR):
        self._require_scope(principal, 'read')
        ident = (body or {}).get('job_id')
        if ident is not None and (not isinstance(ident, str) or not ID.fullmatch(ident)):
            raise ValueError('Choose a valid job ID.')
        executor = self.executor
        operator = 'operator' in principal.scopes
        status = executor.status(ident, recent=operator)
        if operator:
            return status
        status.pop('module_lease_id', None)
        if status['job'] is not None and not self._client_owns_job(status['job']['job_id'], principal):
            status['job'] = None
        for name in ('active_job_id', 'latest_job_id', 'latest_manual_job_id'):
            if not self._client_owns_job(status[name], principal):
                status[name] = None
        if isinstance(principal, ServicePrincipal):
            # Calibration needs the existing controller and recovery evidence for its own jobs.
            check = status['operator_check']
            if check and check['job_id'] and not self._client_owns_job(check['job_id'], principal):
                status['operator_check'] = dict(reason='The executor requires an Operator check.', location=None,
                                                basket=None, job_id=None, step=None, technician=check['technician'])
            alarm = status['quality_alarm']
            if alarm and not self._client_owns_job(alarm['job_id'], principal):
                status['quality_alarm'] = None
            return status
        with executor.guard:
            lease = self.ownership.lease
            status['lease_valid'] = (status['owner'] == principal.name and lease is not None
                                     and lease.principal.token_id == principal.token_id and executor.clock() < lease.deadline)
        for name in ('session', 'recovery_ids', 'evidence_faults', 'operator_check', 'quality_alarm'):
            status.pop(name)
        device = status['device']
        status['device'] = {key: device.get(key) for key in ('connected', 'stale', 'status', 'status_age_s', 'battery_trend')}
        return status

    def heartbeat(self, body, *, principal=OPERATOR):
        self._require_scope(principal, 'jobs')
        executor = self.executor
        if isinstance(principal, ServicePrincipal):
            return executor.heartbeat(body)
        if not set(body) <= MODULE_REQUEST_FIELDS['heartbeat']:
            raise ValueError('The request contains an unsupported field.')
        self.revalidate(principal)
        with executor.guard:
            lease = self._module_lease(body, principal)
            sequence = body.get('sequence')
            if type(sequence) is not int or sequence < 0:
                raise ValueError('Heartbeat needs a nonnegative sequence.')
            if sequence > lease.sequence:
                lease.sequence = sequence
                lease.deadline = executor.clock() + MODULE_LEASE_S
            return dict(lease_valid=True, boot=executor.boot)

    def stop(self, body=None, *, principal=OPERATOR):
        self._require_scope(principal, None)
        if not set(body or {}) <= {'stop_id'} and isinstance(principal, ModulePrincipal):
            raise ValueError('The Stop request contains an unsupported field.')
        self.revalidate(principal)
        source = None if 'operator' in principal.scopes else principal.token_id
        return self.executor.stop(body, source=source) if source is not None else self.executor.stop(body)

    def __call__(self, operation, body=None, *, principal='operator', owner='agv-page'):
        if principal == 'operator':
            if owner == 'calibration':
                principal = ServicePrincipal(owner, OPERATOR.token_id, frozenset(SCOPES))
            elif owner == 'agv-page' or operation == 'stop':
                principal = OPERATOR
            else:
                raise ValueError('Choose agv-page or calibration as the built-in owner.')
        body = {} if body is None else body
        if operation == 'stop':
            return self.stop(body, principal=principal)
        if operation in ('raw', 'raw/undo'):
            self._require_scope(principal, 'raw')
            return self.raw_access.request(body, principal, undo=operation == 'raw/undo')
        if operation in READ_OPERATIONS:
            self._require_scope(principal, 'read')
            if operation == 'jobs':
                return self.list(principal=principal)
            return self.status(body, principal=principal) if operation == 'status' else self.executor.health()
        if operation in MODULE_REQUEST_FIELDS:
            return getattr(self, operation)(body, principal=principal)
        self._require_scope(principal, 'operator')
        return self.executor.dispatch(operation, body)
