"""Event-gated fake SDK workers exercise ownership without physical hardware."""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from anodizing_web.jobs import JobOwner
from anodizing_web.teaching import _abort_and_drain


class JobCleanupTests(unittest.TestCase):
    def abort_with_live_workers(self, fail_storage=False, close_error=True):
        with tempfile.TemporaryDirectory() as tmp:
            owner = JobOwner(tmp)
            gates = [threading.Event() for _ in range(3)]
            workers = [threading.Thread(target=gate.wait, daemon=True) for gate in gates]
            for worker in workers:
                worker.start()
            joins = [worker.join for worker in workers]
            draining = [threading.Event() for _ in workers]
            checkpoint, finished = threading.Event(), threading.Event()
            service = SimpleNamespace(worker_thread=workers[0], motion_thread=workers[1],
                motion=SimpleNamespace(_worker=workers[2], cleanup_uncertain=False),
                stop=lambda: None,
                close=mock.Mock(side_effect=RuntimeError('SDK close timed out') if close_error else None))
            owner.teacher_service = service
            owner.teacher_job = 'aborted-teacher-job'
            original_save = owner._save

            def save(job):
                try:
                    if fail_storage and job.get('execution_uncertain'):
                        raise OSError('injected storage failure')
                    original_save(job)
                finally:
                    if job.get('finished'):
                        finished.set()
                        checkpoint.set()

            def joiner(index):
                def join(timeout=None):
                    if timeout is None:
                        draining[index].set()
                        checkpoint.set()
                        return joins[index]()
                    # A bounded wait expires while this SDK gate is closed.
                return join

            def abort(job_id):
                _abort_and_drain(owner, service, job_id, ValueError('injected teaching abort'))
                raise ValueError('injected teaching abort')

            with ExitStack() as patches:
                patches.enter_context(mock.patch.object(owner, '_save', side_effect=save))
                for index, worker in enumerate(workers):
                    patches.enter_context(mock.patch.object(worker, 'join', side_effect=joiner(index)))
                try:
                    owner.submit('aborted-teacher-job', 'teaching', {'action': 'configure'}, abort)
                    self.assertTrue(checkpoint.wait(3), 'Cleanup must drain or reach job finalization')
                    with owner.lock:
                        self.assertTrue(all(worker.is_alive() for worker in workers))
                        self.assertEqual(owner.active, 'aborted-teacher-job')
                        self.assertTrue(owner.execution_uncertain)
                        self.assertTrue(owner.jobs[owner.active]['execution_uncertain'])
                        self.assertIs(owner.teacher_service, service)
                    if fail_storage:
                        self.assertIn('injected storage failure', owner.storage_error)
                    else:
                        saved = json.loads((Path(tmp) / '.jobs/aborted-teacher-job.json').read_text())
                        self.assertTrue(saved['execution_uncertain'])
                    with self.assertRaises(ValueError):
                        owner.submit('competing-capture', 'teaching', {'action': 'capture'}, lambda _: None)
                    with self.assertRaisesRegex(ValueError, 'owns the service|storage failed'):
                        owner.submit('competing-config', 'teaching', {'action': 'configure'}, lambda _: None)
                    for index, gate in enumerate(gates):
                        self.assertTrue(draining[index].wait(3))
                        with owner.lock:
                            self.assertEqual(owner.active, 'aborted-teacher-job')
                        gate.set()
                        joins[index](3)
                        self.assertFalse(workers[index].is_alive())
                    self.assertTrue(finished.wait(3))
                    with owner.lock:
                        self.assertIsNone(owner.active)
                        self.assertIsNone(owner.teacher_service)
                        self.assertEqual(owner.jobs['aborted-teacher-job']['state'], 'failed')
                    if not fail_storage:
                        self.assertTrue(JobOwner(tmp).execution_uncertain)
                finally:
                    for gate in gates:
                        gate.set()
                    for join in joins:
                        join(3)
                    finished.wait(3)

    def test_teaching_abort_retains_ownership_until_all_workers_stop(self):
        self.abort_with_live_workers()

    def test_storage_failure_does_not_release_live_workers(self):
        self.abort_with_live_workers(fail_storage=True)

    def test_live_workers_are_fenced_even_when_close_returns_successfully(self):
        self.abort_with_live_workers(close_error=False)
