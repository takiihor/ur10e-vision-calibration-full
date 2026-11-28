"""ECB-306 tower display. Only the display worker writes during execution."""
import argparse
from contextlib import closing, contextmanager
import fcntl
import logging
import os
from pathlib import Path
import re
import signal
import termios
from threading import Event, Thread
from time import monotonic, sleep

RED, YELLOW, GREEN, BUZZER = 1, 2, 4, 8
PATTERNS = {
    'recovery': (RED, True),
    'alarm': (RED, False),
    'moving': (YELLOW, True),
    'armed': (YELLOW, False),
    'ready': (GREEN, False),
    'off': (0, False),
}
POLL_S = .05
LOG = logging.getLogger(__name__)


def feedback_moving(report):
    current, target = report.get('current_station'), report.get('target_station')
    speed = report.get('actual_speed_m_h')
    return (report.get('stopped') is False or (type(speed) in (int, float) and speed > 0)
            or (type(current) is int and type(target) is int and target > 0 and current != target))


def indication(*, ready=False, armed=False, moving=False, stopped=False,
               checking=False, fault=False, loss=False):
    return ('recovery' if loss else 'alarm' if stopped or checking or fault else
            'moving' if moving else 'armed' if armed else 'ready' if ready else 'off')


class Pattern:
    def __init__(self):
        self.pattern = 'off'
        self.since = 0
        self.moving = False
        self.alarms = set()
        self.beep = float('-inf')
        self.beep_count = 0

    def sample(self, now, **inputs):
        alarms = {name for name in ('stopped', 'checking', 'fault') if inputs.get(name)}
        pattern = indication(**inputs)
        moving = inputs.get('moving', False)
        if pattern != self.pattern:
            self.pattern, self.since = pattern, now
        if alarms - self.alarms:
            self.beep, self.beep_count = now, 3
        elif pattern == 'moving' and not self.moving:
            self.beep, self.beep_count = now, 1
        self.alarms, self.moving = alarms, moving
        mask, flashing = PATTERNS[pattern]
        if flashing and (now - self.since) % 1 >= .5:
            mask = 0
        elapsed = now - self.beep
        if elapsed < .4 * self.beep_count and elapsed % .4 < .2:
            mask |= BUZZER
        return pattern, mask


def enabled():
    return os.environ.get('ANODIZING_TOWER_LIGHT_ENABLED') == '1'


def active_low():
    return os.environ.get('ANODIZING_TOWER_LIGHT_ACTIVE_LOW') == '1'


def open_serial():
    import serial

    device = Path(os.environ.get('ANODIZING_TOWER_LIGHT_DEVICE', '/dev/ttyACM0')).resolve()
    if device.parent != Path('/dev') or not re.fullmatch(r'ttyACM[0-9]+', device.name):
        raise ValueError('Use an ECB-306 ttyACM device or its stable /dev/serial symlink.')
    if not device.is_char_device():
        raise OSError('ECB-306 serial device unavailable.')
    port = serial.Serial(str(device), 115200, timeout=.2, write_timeout=.2, exclusive=True)
    try:
        fcntl.ioctl(port.fileno(), termios.TIOCEXCL)
    except OSError:
        port.close()
        raise
    return port


def write_frame(port, frame):
    if port.write(frame) != len(frame):
        raise OSError('Short ECB-306 command write.')


def write_mask(port, inverted, mask):
    # The 2026-11-27 attended hardware check verified GW01/02/04/08 and GW00 on the ECB-306.
    frame = f'GW{mask ^ (0x0f if inverted else 0):02X}\r'.encode('ascii')
    write_frame(port, frame)


@contextmanager
def outputs(open_port, inverted):
    with closing(open_port()) as port:
        def write(mask):
            write_mask(port, inverted, mask)

        write(0)
        try:
            write_frame(port, b'GR\r')
            if not re.fullmatch(rb'GR[0-9A-Fa-f]{2}\r', port.read(5)):
                raise OSError('Missing or invalid ECB-306 input reply.')
            yield write
        finally:
            write(0)


class TowerLight:
    def __init__(self, open_port, inverted):
        self.open_port, self.inverted = open_port, inverted
        self.closing = Event()
        self.current = False, 'unavailable'
        self.worker = None

    def start(self, read):
        try:
            worker = Thread(target=self.run, args=(read,), daemon=True, name='executor-tower-light')
            worker.start()
        except Exception:
            LOG.exception('Tower light worker failed to start.')
            return
        self.worker = worker

    def status(self):
        available, pattern = self.current
        return dict(available=available, pattern=pattern)

    def run(self, read):
        pattern = Pattern()
        while not self.closing.is_set():
            try:
                with outputs(self.open_port, self.inverted) as write:
                    last_mask = 0
                    while not self.closing.is_set():
                        state, mask = pattern.sample(monotonic(), **read())
                        if mask != last_mask:
                            try:
                                write(mask)
                            except OSError:
                                self.current = False, 'unavailable'
                                raise
                            last_mask = mask
                        self.current = True, state
                        self.closing.wait(POLL_S)
            except (OSError, ValueError, ImportError):
                self.current = False, 'unavailable'
                self.closing.wait(1)

    def close(self):
        self.closing.set()
        if self.worker is not None:
            self.worker.join(.75)


def lamp_test(write, *, sleep=sleep):
    for mask in (RED, YELLOW, GREEN, BUZZER):
        write(mask)
        sleep(1)


def main():
    parser = argparse.ArgumentParser(description='IPC tower-light maintenance')
    parser.add_argument('operation', choices=('off', 'test'))
    operation = parser.parse_args().operation
    try:
        if operation == 'off':
            if enabled():
                with closing(open_serial()) as port:
                    write_mask(port, active_low(), 0)
            return 0
        if os.geteuid() != 0:
            parser.exit(1, 'The tower-light test requires an IPC administrator.\n')
        data = Path(os.environ.get('ANODIZING_DATA_DIR', '/var/lib/anodizing-executor/data'))
        signal.signal(signal.SIGTERM, signal.default_int_handler)
        with (data / '.executor.lock').open('r+') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                parser.exit(1, 'Stop the idle IPC executor before running the tower-light test.\n')
            with outputs(open_serial, active_low()) as write:
                lamp_test(write)
    except (OSError, ValueError, ImportError) as exc:
        parser.exit(1, f'Tower light unavailable: {exc}\n')
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
