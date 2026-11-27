"""Read-only Katana line-link diagnosis."""
from datetime import datetime, timezone
from ipaddress import IPv4Address
from pathlib import Path
from sys import byteorder


HINTS = {
    'adapter_no_link': ("Katana's line Wi-Fi has no link. Unplug the USB Wi-Fi dongle and plug it back in. "
                        "If the link is still lost after 1 minute, reboot Katana."),
    'executor_not_answering': 'The IPC on the AGV is not answering. Check that the AGV is powered on.',
}


def line_carrier(address, routes=Path('/proc/net/route'), interfaces=Path('/sys/class/net')):
    try:
        destination = int.from_bytes(IPv4Address(address).packed, byteorder)
    except ValueError:
        destination = None
    interface, prefix = None, -1

    def read():
        nonlocal interface, prefix
        if destination is None:
            return None
        try:
            matches = []
            for row in routes.read_text().splitlines()[1:]:
                name, network, _, flags, _, _, metric, mask, *_ = row.split()
                mask = int(mask, 16)
                if int(flags, 16) & 1 and destination & mask == int(network, 16):
                    matches.append((mask.bit_count(), -int(metric), name))
            if matches:
                length, _, name = max(matches)
                # A vanished line route can leave a default route via another adapter.
                if length or prefix <= 0:
                    interface, prefix = name, length
        except (OSError, ValueError):
            pass
        if interface is None:
            return None
        device = interfaces / interface
        try:
            return {'0': False, '1': True}[device.joinpath('carrier').read_text().strip()]
        except (OSError, KeyError):
            try:
                return {'down': False, 'lowerlayerdown': False, 'up': True}.get(
                    device.joinpath('operstate').read_text().strip())
            except FileNotFoundError:
                return False if not device.exists() else None
            except OSError:
                return None

    return read


class LineLink:
    def __init__(self, read_carrier):
        self.read_carrier = read_carrier
        self.next_sample = 0
        self.state = None

    def sample(self, reachable, now):
        if now < self.next_sample:
            return
        self.next_sample = now + 5
        carrier = self.read_carrier()
        if reachable is None and carrier is not False:
            return
        cause = ('adapter_no_link' if carrier is False else
                 'executor_not_answering' if reachable is False else None)
        state = 'lost' if cause else 'ok'
        if self.state and self.state['state'] == state:
            since = self.state['since']
        else:
            since = datetime.now(timezone.utc).isoformat(timespec='seconds') if cause or self.state else None
        self.state = dict(state=state, since=since, cause=cause, hint=HINTS.get(cause))

    def failure_message(self):
        hint = (self.state or {}).get('hint') or HINTS['executor_not_answering']
        return f'AGV link lost. {hint}'
