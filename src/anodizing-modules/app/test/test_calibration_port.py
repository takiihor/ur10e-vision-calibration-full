"""Calibration cancellation and output identity, using offline workers and data."""
import json
from pathlib import Path
from threading import Event
import time

import pytest

from anodizing_web import calibration
from anodizing_web.jobs import JobOwner


def test_stop_before_calibration_worker_handoff(tmp_path):
    owner = JobOwner(tmp_path)
    entered, release = Event(), Event()
    job_id = 'calibration-manual'

    def dispatch(ident):
        entered.set()
        assert release.wait(3)
        calibration.check_stop(owner, ident)
        pytest.fail('A stopped capture must not start.')

    owner.submit(job_id, 'calibration', {'action': 'manual'}, dispatch)
    try:
        assert entered.wait(3)
        assert owner.status()['stop_targets'] == [{'kind': 'calibration', 'job_id': job_id}]
        with pytest.raises(ValueError, match='active'):
            owner.stop_calibration('an-old-job')
        assert owner.stop_calibration(job_id)['stop_requested']
        assert owner.stop_calibration(job_id)['stop_requested']
        with pytest.raises(ValueError, match='no longer active|stopping'):
            owner.answer(job_id, 1, '')
        with pytest.raises(ValueError, match='owns the service'):
            owner.submit('other-capture', 'calibration', {'action': 'manual'}, dispatch)
    finally:
        release.set()
        deadline = time.monotonic() + 3
        while owner.status()['active'] and time.monotonic() < deadline:
            time.sleep(.01)
    assert owner.get(job_id)['state'] == 'interrupted'
    assert JobOwner(tmp_path).get(job_id)['stop_requested']
    with pytest.raises(ValueError, match='active'):
        owner.stop_calibration(job_id)


def test_handoff_checks_selected_result_and_its_bytes(tmp_path):
    from ur_handeye.dataset import load_result

    path = tmp_path / 'calibration.json'
    fixture = Path(__file__).parent / 'fixtures' / 'simulated_calibration.json'
    source = json.loads(fixture.read_text())
    source['verdict'] = 'acceptable'
    source['board_in_base'] = {'matrix': [[int(i == j) for j in range(4)] for i in range(4)]}
    path.write_text(json.dumps(source))
    saved = {'id': 'calibration-fit', 'module': 'calibration', 'state': 'done',
             'result': {'path': str(path), 'result': load_result(path),
                        'calibration_sha256': calibration.artifact_sha256(path)}}
    handoff = calibration.teaching_handoff(tmp_path, saved, path.name)
    assert handoff['simulation'] and handoff['calibration'] == str(path)
    assert handoff['calibration_sha256'] == saved['result']['calibration_sha256']
    with pytest.raises(ValueError, match='selected'):
        calibration.teaching_handoff(tmp_path, saved, 'different.json')
    path.write_text(json.dumps(source, indent=2))
    with pytest.raises(ValueError, match='changed'):
        calibration.teaching_handoff(tmp_path, saved, path.name)
