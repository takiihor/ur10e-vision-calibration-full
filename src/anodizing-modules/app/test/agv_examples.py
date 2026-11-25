"""Invented examples encoded with the production frame encoder."""
from anodizing_web import agv


EXAMPLE_SPEED = 1337
EXAMPLE_PREFIX = bytes.fromhex('42 19 0D')
EXAMPLE_REPLY_PREFIX = bytes.fromhex('00 FF 0D')
EXAMPLE_ROUTE = {'address': '42 19', 'channel': '0D'}
EXAMPLE_VEHICLE_ID = EXAMPLE_PREFIX[1]
EXAMPLE_STATUS = bytes((EXAMPLE_VEHICLE_ID, 83)) + EXAMPLE_SPEED.to_bytes(2, 'little') + bytes((
    1, 0, 1, 0, 0, 0, 7, 9, 0, 12, 2, 5, 9, 8, 0, 0, 0, 0, 1, 0, 1, 24,
))
REPORT = agv.Frame(0x25, EXAMPLE_STATUS).encode()


def synthetic_replies(prefix):
    broadcast = agv.reply_prefix(prefix)
    return (
        agv.Frame(0x25, EXAMPLE_STATUS[:-2], broadcast).encode(),
        agv.Frame(0x21, EXAMPLE_SPEED.to_bytes(2, 'little'), broadcast).encode(),
        agv.Frame(0x01, b'').encode(),
    )
