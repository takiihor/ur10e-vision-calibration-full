import fcntl
from pathlib import Path
import shlex
import sys
from threading import Event, Thread
from time import monotonic, sleep

import pytest

from anodizing_web import agv, tower_light
from anodizing_web.executor import Executor, FakeDevices
from anodizing_web.executor_http import WiredDevices


class FakeSerial:
    def __init__(self, reply=b'GR00\r'):
        self.frames = []
        self.closed = False
        self.reply = reply

    def write(self, frame):
        self.frames.append(frame)
        return len(frame)

    def close(self):
        self.closed = True

    def read(self, size):
        return self.reply[:size]


@pytest.mark.parametrize('active_low,frames', [
    (False, [b'GW00\r', b'GR\r', b'GW01\r', b'GW02\r', b'GW04\r', b'GW08\r', b'GW00\r']),
    (True, [b'GW0F\r', b'GR\r', b'GW0E\r', b'GW0D\r', b'GW0B\r', b'GW07\r', b'GW0F\r']),
])
def test_lamp_test_frames_and_polarity(active_low, frames):
    serial = FakeSerial()
    waits = []
    with tower_light.outputs(lambda: serial, active_low) as write:
        tower_light.lamp_test(write, sleep=waits.append)
    assert serial.frames == frames
    assert serial.closed
    assert waits == [1, 1, 1, 1]


def test_operator_interrupt_clears_the_buzzer():
    serial = FakeSerial()
    with pytest.raises(KeyboardInterrupt):
        with tower_light.outputs(lambda: serial, False) as write:
            write(tower_light.BUZZER)
            raise KeyboardInterrupt
    assert serial.frames == [b'GW00\r', b'GR\r', b'GW08\r', b'GW00\r']
    assert serial.closed


def test_crash_hook_clears_only_when_enabled(monkeypatch):
    serial = FakeSerial()
    monkeypatch.setattr(sys, 'argv', ['anodizing-tower-light', 'off'])
    monkeypatch.setattr(tower_light, 'open_serial', lambda: serial)
    monkeypatch.delenv('ANODIZING_TOWER_LIGHT_ENABLED', raising=False)
    monkeypatch.delenv('ANODIZING_TOWER_LIGHT_ACTIVE_LOW', raising=False)
    assert tower_light.main() == 0
    assert serial.frames == []
    monkeypatch.setenv('ANODIZING_TOWER_LIGHT_ENABLED', '1')
    assert tower_light.main() == 0
    assert serial.frames == [b'GW00\r']
    assert serial.closed


def test_crash_hook_deadline_includes_its_kill_grace():
    unit = Path(__file__).resolve().parents[2] / 'tools/anodizing-executor-tower-light.conf'
    hook = next(line.removeprefix('ExecStopPost=') for line in unit.read_text().splitlines()
                if line.startswith('ExecStopPost='))
    timeout, option, kill_grace, term_at, program, command = shlex.split(hook)
    assert timeout == '-/usr/bin/timeout' and option == '-k'
    assert program.endswith('/anodizing-tower-light') and command == 'off'
    assert float(term_at.removesuffix('s')) + float(kill_grace.removesuffix('s')) <= 2


def test_lamp_test_requires_admin_and_an_idle_executor_lock(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', ['anodizing-tower-light', 'test'])
    monkeypatch.setenv('ANODIZING_DATA_DIR', str(tmp_path))
    serial = FakeSerial()
    monkeypatch.setattr(tower_light, 'open_serial', lambda: serial)
    monkeypatch.setattr(tower_light.os, 'geteuid', lambda: 1000)
    with pytest.raises(SystemExit, match='1'):
        tower_light.main()
    assert 'requires an IPC administrator' in capsys.readouterr().err
    monkeypatch.setattr(tower_light.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(tower_light.signal, 'signal', lambda *args: None)
    with (tmp_path / '.executor.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with pytest.raises(SystemExit, match='1'):
            tower_light.main()
    assert 'Stop the idle IPC executor' in capsys.readouterr().err
    assert serial.frames == []


def test_signal_state_and_alarm_timing():
    light = tower_light.Pattern()
    assert light.sample(0) == ('off', 0)
    assert light.sample(1, ready=True) == ('ready', 4)
    assert light.sample(2, armed=True) == ('armed', 2)
    assert light.sample(3, moving=True) == ('moving', 2 | 8)
    assert light.sample(3.25, moving=True) == ('moving', 2)
    assert light.sample(3.55, moving=True) == ('moving', 0)
    assert light.sample(4.05, moving=True) == ('moving', 2)
    assert light.sample(5, stopped=True) == ('alarm', 1 | 8)
    assert light.sample(5.25, stopped=True) == ('alarm', 1)
    assert light.sample(5.45, stopped=True) == ('alarm', 1 | 8)
    assert light.sample(5.85, stopped=True) == ('alarm', 1 | 8)
    assert light.sample(6.10, stopped=True) == ('alarm', 1)
    assert light.sample(7, stopped=True, checking=True, loss=True) == ('recovery', 1 | 8)
    assert light.sample(7.55, stopped=True, checking=True, loss=True) == ('recovery', 8)
    assert light.sample(8.3, stopped=True, checking=True, loss=True) == ('recovery', 1)
    assert light.sample(9, fault=True) == ('alarm', 1 | 8)
    assert light.sample(10.1, fault=True) == ('alarm', 1)


def test_busy_until_dispatch_and_loss_requires_recovery(tmp_path):
    devices = WiredDevices(tmp_path, lambda *args: None)
    executor = Executor(tmp_path, devices, clock=lambda: 12)
    link = devices.link
    try:
        assert not executor._tower_inputs()['ready']
        link.transport = FakeSerial()
        assert not executor._tower_inputs()['ready']
        link.status_at = monotonic()
        link.status = dict(stopped=True, fault_code=0, obstacle_stop=False, actual_speed_m_h=0)
        assert executor._tower_inputs()['ready']
        executor.deadline = 20
        executor.armed = True
        executor.module_attestation = ({}, 15)
        assert executor._tower_inputs()['armed']
        link.recipe_active = True
        link.recipe_call_at = None
        assert not executor._tower_inputs()['moving']
        link.recipe_call_at = monotonic()
        assert executor._tower_inputs()['moving']
        link.recipe_active = False
        assert not executor._tower_inputs()['moving']
        link.transport = None
        link.error = agv.LINK_LOST
        assert not executor._tower_inputs()['ready']
        assert not executor._tower_inputs()['loss']
        executor._hold('Verify the hold.')
        assert executor._tower_inputs()['loss']
        link.error = None
        executor.check = None
        executor._hold('Module lease expired.', link_loss=True)
        assert executor._tower_inputs()['loss']
    finally:
        executor.close()


def test_display_snapshot_does_not_acquire_the_agv_link_lock(tmp_path):
    devices = WiredDevices(tmp_path, lambda *args: None)
    snapshot = []
    reader = Thread(target=lambda: snapshot.append(devices.tower_feedback()))
    try:
        with devices.link.lock:
            reader.start()
            reader.join(.5)
            assert snapshot and not snapshot[0]['connected']
    finally:
        reader.join()
        devices.close(final=True)


def test_remote_call_signals_motion_before_the_agv_leaves_its_station(tmp_path):
    devices = WiredDevices(tmp_path, lambda *args: None)
    executor = Executor(tmp_path, devices)
    link = devices.link
    light = tower_light.Pattern()

    def report(status):
        link.status = status
        link.status_at = monotonic()

    try:
        link.transport = FakeSerial()
        at_station = dict(stopped=True, fault_code=0, obstacle_stop=False, actual_speed_m_h=0,
                          current_station=3, target_station=3)
        report(at_station)
        assert light.sample(0, **executor._tower_inputs()) == ('ready', tower_light.GREEN)

        report(at_station | {'target_station': 4})
        assert not link.recipe_active and executor.owner.active is None
        display = executor.status(display=True)
        assert display['display_pattern'] == 'moving' and display['job'] is None
        assert display['device']['status']['target_station'] == 4
        assert light.sample(1, **executor._tower_inputs()) == (
            'moving', tower_light.YELLOW | tower_light.BUZZER)

        report(at_station | {'target_station': 4, 'stopped': False, 'actual_speed_m_h': 10})
        assert light.sample(1.25, **executor._tower_inputs()) == ('moving', tower_light.YELLOW)
        assert light.sample(1.75, **executor._tower_inputs()) == ('moving', 0)

        report(at_station | {'current_station': 4, 'target_station': 4})
        assert light.sample(2, **executor._tower_inputs()) == ('ready', tower_light.GREEN)
    finally:
        executor.close()


def test_obstacle_stop_in_fresh_agv_feedback_signals_alarm(tmp_path):
    devices = WiredDevices(tmp_path, lambda *args: None)
    executor = Executor(tmp_path, devices)
    try:
        devices.link.transport = FakeSerial()
        devices.link.status_at = monotonic()
        devices.link.status = dict(stopped=True, actual_speed_m_h=0, fault_code=0,
                                   obstacle_stop=True, current_station=3, target_station=4)
        light = tower_light.Pattern()
        assert light.sample(0, **executor._tower_inputs()) == (
            'alarm', tower_light.RED | tower_light.BUZZER)
    finally:
        executor.close()


@pytest.mark.parametrize('connected', [False, True], ids=['disconnected', 'stale'])
def test_green_requires_fresh_agv_feedback(tmp_path, connected):
    devices = WiredDevices(tmp_path, lambda *args: None)
    executor = Executor(tmp_path, devices)
    try:
        link = devices.link
        if connected:
            link.transport = FakeSerial()
        link.status_at = monotonic() - agv.RECIPE_STALE_S
        link.status = dict(stopped=True, actual_speed_m_h=0, fault_code=0,
                           obstacle_stop=False, current_station=3, target_station=3)
        light = tower_light.Pattern()
        assert light.sample(0, **executor._tower_inputs()) == ('off', 0)
    finally:
        executor.close()


def test_stalled_lamp_write_does_not_delay_stop_and_is_reported(tmp_path):
    opened, release, stopped = Event(), Event(), Event()

    class BrokenSerial(FakeSerial):
        def write(self, frame):
            opened.set()
            release.wait(3)
            raise OSError('USB unavailable')

    class Devices(FakeDevices):
        def tower_feedback(self):
            return dict(fresh=False, report={}, link_lost=False, moving=False)

        def stop(self):
            stopped.set()
            return super().stop()

    serial = BrokenSerial()
    light = tower_light.TowerLight(lambda: serial, False)
    executor = Executor(tmp_path, Devices(), tower=light)
    try:
        assert opened.wait(1)
        result = []
        call = Thread(target=lambda: result.append(executor.stop()))
        call.start()
        assert stopped.wait(1)
        call.join(1)
        assert result[0]['received']
        release.set()
        deadline = monotonic() + 1
        while not serial.closed and monotonic() < deadline:
            sleep(.01)
        assert serial.closed
        assert executor.status(display=True)['tower_light'] == {'available': False, 'pattern': 'unavailable'}
    finally:
        release.set()
        executor.close()


def test_executor_shutdown_clears_outputs(tmp_path):
    serial = FakeSerial()

    class Devices(FakeDevices):
        def tower_feedback(self):
            report = dict(stopped=True, fault_code=0, obstacle_stop=False, actual_speed_m_h=0)
            return dict(fresh=True, report=report, link_lost=False, moving=False)

    executor = Executor(tmp_path, Devices(), tower=tower_light.TowerLight(lambda: serial, False))
    try:
        deadline = monotonic() + 1
        while executor.status()['tower_light']['pattern'] != 'ready' and monotonic() < deadline:
            sleep(.01)
        assert executor.status()['tower_light'] == {'available': True, 'pattern': 'ready'}
    finally:
        executor.close()
    assert serial.frames == [b'GW00\r', b'GR\r', b'GW04\r', b'GW00\r']
    assert serial.closed


@pytest.mark.parametrize('reply', [b'', b'GR0Z\r'])
def test_failed_input_probe_leaves_display_unavailable(reply):
    serial = FakeSerial(reply)
    light = tower_light.TowerLight(lambda: serial, False)
    light.start(lambda: dict(ready=True))
    try:
        deadline = monotonic() + 1
        while not serial.closed and monotonic() < deadline:
            sleep(.01)
        assert light.status() == {'available': False, 'pattern': 'unavailable'}
    finally:
        light.close()
    assert serial.frames == [b'GW00\r', b'GR\r', b'GW00\r']
    assert serial.closed
