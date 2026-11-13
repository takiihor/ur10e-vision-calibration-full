"""Fake SDK constructor side effects only; never imports an installed SDK."""
import builtins
import os
from types import SimpleNamespace
import unittest
from unittest import mock

from anodizing_web.teacher_motion import native_backend
from ur_pick_place.vision.cli import Runtime


class TeacherSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.policy,self.priority=0,0
        self.starts,self.cleaned=[],[]
        self.failure=None
        self.deny_restore=False
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(os,'sched_getscheduler',side_effect=lambda tid:self.policy).start()
        mock.patch.object(os,'sched_getparam',side_effect=lambda tid:SimpleNamespace(sched_priority=self.priority)).start()
        def restore(tid,policy,param):
            if self.deny_restore:
                raise PermissionError('injected scheduler restore failure')
            self.policy,self.priority=policy,param.sched_priority
        mock.patch.object(os,'sched_setscheduler',side_effect=restore).start()
        def constructor(kind):
            def acquire(host,frequency):
                assert host=='fake-only.invalid'
                self.starts.append((kind,self.policy,self.priority))
                self.policy,self.priority=1,90
                if self.failure==kind:
                    raise RuntimeError(kind+' refused')
                return SimpleNamespace(stopScript=lambda:self.cleaned.append(('stop',self.policy,self.priority)),
                    disconnect=lambda:self.cleaned.append((kind,self.policy,self.priority)))
            return acquire
        modules={'rtde_control':SimpleNamespace(RTDEControlInterface=constructor('control')),
                 'rtde_receive':SimpleNamespace(RTDEReceiveInterface=constructor('receive'))}
        original=builtins.__import__
        def fake_import(name,*args,**kwargs):
            return modules[name] if name in modules else original(name,*args,**kwargs)
        mock.patch.object(builtins,'__import__',side_effect=fake_import).start()

    def test_teacher_control_and_receive_restore_each_original_policy(self):
        self.policy,self.priority=1,42
        control,receive=native_backend('fake-only.invalid')
        self.assertEqual(self.starts,[('control',1,42),('receive',1,42)])
        self.assertEqual((self.policy,self.priority),(1,42))
        control.disconnect();receive.disconnect()

    def test_receive_failure_restores_before_cleaning_known_control(self):
        self.failure='receive'
        with self.assertRaisesRegex(RuntimeError,'receive refused'):
            native_backend('fake-only.invalid')
        self.assertEqual((self.policy,self.priority),(0,0))
        self.assertEqual(self.cleaned,[('stop',0,0),('control',0,0)])

    def test_native_capture_receive_failure_restores_caller(self):
        self.failure='receive'
        with self.assertRaisesRegex(RuntimeError,'receive refused'):
            Runtime.receive('fake-only.invalid')
        self.assertEqual((self.policy,self.priority),(0,0))
        self.assertEqual(self.starts,[('receive',0,0)])

    def test_restore_failure_fails_closed_and_cleans_acquired_channel(self):
        self.deny_restore=True
        with self.assertRaisesRegex(PermissionError,'scheduler restore failure'):
            Runtime.receive('fake-only.invalid')
        self.assertEqual([item[0] for item in self.cleaned],['receive'])
