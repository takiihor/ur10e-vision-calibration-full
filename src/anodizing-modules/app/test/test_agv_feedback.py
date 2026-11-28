"""Guard the Katana wording against changes in deployed refusal sources."""
import ast
from pathlib import Path

import pytest

from anodizing_web import agv_feedback


SOURCE = Path(__file__).resolve().parents[1] / 'anodizing_web'
# Include the service, adapter and IPC failures the two pages can encounter.
SCOPES = {
    'agv': None, 'agv_ble': None, 'executor_link': None, 'executor': None,
    'executor_http': None, 'job_gateway': None, 'recipe_agv': None,
    'jobs': {'submit'}, 'travel_pose': {'motion_block', '_load', '_check', '_sample'},
    'agv_status': {'motion_block', 'port_failure'},
    'executor_link_status': {'_motion_reason', 'bluetooth_write_block'},
    'server': {'OperatorActions', '_bluetooth_write_block', 'bluetooth_status', '_executor_status',
               '_executor_action', 'read_body', 'do_POST'},
}
RAISES = {'ValueError', 'PermissionError', 'OSError', 'FileNotFoundError',
          'AdmissionRefused', 'JobEvidenceError', 'ReceiveError'}


def examples(node, constants):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        yield node.value
    elif isinstance(node, ast.Name) and node.id in constants:
        yield from examples(constants[node.id], {})
    elif isinstance(node, ast.JoinedStr) and not any(
            isinstance(part, ast.FormattedValue) and isinstance(part.value, ast.Name) and part.value.id == 'exc'
            for part in node.values):
        if any(isinstance(part, ast.FormattedValue) and
               ast.unparse(part.value) in ('self.owner.storage_error', 'PORT_RETRY') for part in node.values):
            return  # These replies wrap dynamic storage details or the existing port guidance.
        yield ''.join(
            part.value if isinstance(part, ast.Constant) else
            '07' if part.format_spec else '7' for part in node.values
        )
    elif isinstance(node, (ast.IfExp, ast.BoolOp)):
        for value in ([node.body, node.orelse] if isinstance(node, ast.IfExp) else node.values):
            yield from examples(value, constants)


def source_reasons():
    for name, scope in SCOPES.items():
        tree = ast.parse((SOURCE / f'{name.removesuffix('_status')}.py').read_text())
        constants = {node.targets[0].id: node.value for node in tree.body
                     if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)}
        roots = tree.body if scope is None else [
            node for node in ast.walk(tree) if isinstance(node, (ast.ClassDef, ast.FunctionDef))
            and node.name in scope
        ]
        reasons = set()
        for root in roots:
            for node in ast.walk(root):
                if (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)
                        and isinstance(node.exc.func, ast.Name) and node.exc.func.id in RAISES
                        and node.exc.args):
                    reasons.update(examples(node.exc.args[-1 if node.exc.func.id == 'AdmissionRefused' else 0], constants))
                if isinstance(node, ast.Return) and (name.endswith('_status') or name == 'travel_pose'):
                    reasons.update(reason for reason in examples(node.value, constants) if reason)
                if isinstance(node, ast.Call):
                    if isinstance(node.func, ast.Attribute) and node.func.attr in {'_hold', '_manual_hold'}:
                        index = int(node.func.attr == '_manual_hold')
                        if len(node.args) > index:
                            reasons.update(examples(node.args[index], constants))
                    for keyword in node.keywords:
                        if keyword.arg in {'error', 'motion_block', 'programme_motion_block', 'doubt'}:
                            reasons.update(examples(keyword.value, constants))
                    if (name == 'server' and isinstance(node.func, ast.Attribute)
                            and node.func.attr == '_reply_action_error' and len(node.args) == 3):
                        reasons.update(examples(node.args[2], constants))
                if isinstance(node, ast.Assign) and name in {'server', 'agv_ble'}:
                    if any(ast.unparse(target) in {'blocker', 'self.route_error'} for target in node.targets):
                        reasons.update(examples(node.value, constants))
                if isinstance(node, ast.Dict):
                    for key, value in zip(node.keys, node.values):
                        if isinstance(key, ast.Constant) and key.value in (
                                'error', 'motion_block', 'programme_motion_block', 'doubt'):
                            reasons.update(examples(value, constants))
        yield name, reasons


def test_every_known_refusal_has_operator_text():
    missing = {name: sorted(reason for reason in reasons if agv_feedback.describe(reason)['code'] == reason)
               for name, reasons in source_reasons()}
    assert not any(missing.values()), '\n'.join(f'{name}: {reason}' for name, reasons in missing.items()
                                               for reason in reasons)


def test_unknown_code_is_visible_with_technician_guidance():
    assert agv_feedback.describe('future_ipc_code') == {
        'code': 'future_ipc_code', 'message': 'Reason reported: future_ipc_code.',
        'hint': 'Tell the technician.',
    }


@pytest.mark.parametrize(('error', 'code', 'values'), [
    ('AGV battery reading is 17. Verify at least 20% before travel.', 'battery_floor', ['17%', '20%']),
    ('Clear AGV fault 61 before manual travel.', 'agv_fault', ['61']),
    ('Choose a station in the learned range (1 to 2) with station-call mode enabled. '
     'The request was for station 4, mode 2.', 'station_range', ['station 4', '1 learned leg', '1 to 2']),
])
def test_executor_refusal_keeps_values(error, code, values):
    feedback = agv_feedback.describe(error, {'status': {'max_stations': 1}})
    assert feedback['code'] == code
    assert all(value in feedback['message'] for value in values)
    assert feedback['hint']


@pytest.mark.parametrize(('connected', 'age', 'stale'), [(True, 0.2, False), (True, 4, True), (False, 0.2, True)])
def test_both_pages_can_use_the_same_wired_reading(connected, age, stale):
    report = dict(value=1, current_station=2, observed_at='2026-11-26T14:45:06Z')
    state = dict(executor_link=True, connected=connected, stale=False, status_age_s=age,
                 status_time=report['observed_at'] if connected else None)
    assert agv_feedback.station_snapshot(report, state) == dict(report, stale=stale)
    assert agv_feedback.station_snapshot(None, state) is None
