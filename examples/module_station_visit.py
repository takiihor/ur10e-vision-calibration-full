#!/usr/bin/env python3
"""Claim the fake gateway, request a visit and wait for its result."""
import argparse
import json
from pathlib import Path
from time import monotonic, sleep
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

from anodizing_web.job_client import DevModule


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('url', help='Fake executor HTTPS endpoint')
    parser.add_argument('key', type=Path, help='Private module key file')
    parser.add_argument('certificate', type=Path, help='Pinned executor certificate')
    parser.add_argument('station', type=int)
    args = parser.parse_args()
    url = urlsplit(args.url)
    if url.scheme != 'https' or url.hostname != '127.0.0.1' or url.path not in ('', '/'):
        parser.error('Use the fake listener at https://127.0.0.1:PORT.')
    module = DevModule(args.url, args.key.read_text().strip(), args.certificate, {'read', 'jobs'})
    call = module.request
    if call('health').get('simulated') is not True:
        parser.error('This example requires the fake executor.')
    claim = call('claim', {})
    lease = {'lease_id': claim['lease_id']}
    sequence = 0
    pending = False

    def renew():
        nonlocal sequence
        sequence += 1
        call('heartbeat', dict(lease, sequence=sequence))

    try:
        owner = claim['owner']
        print(f'Claimed {owner}. Connect and Arm for {owner} on the fake AGV page.')
        deadline = monotonic() + 60
        while True:
            renew()
            status = call('status')
            if status.get('armed_for') == owner:
                break
            if monotonic() >= deadline:
                raise SystemExit('Timed out waiting for the fake operator Arm.')
            sleep(.5)
        job_id = uuid4().hex
        pending = True
        call('submit', dict(lease, job_id=job_id, type='station_visit', version=1,
                            stop_generation=status['stop_generation'], params={'station': args.station}))
        deadline = monotonic() + 60
        while True:
            renew()
            status = call('status?' + urlencode({'job_id': job_id}))
            job = status['job']
            if status['active_job_id'] is None and job['state'] not in ('accepted', 'running', 'waiting'):
                pending = False
                print(json.dumps(job, indent=2))
                break
            if monotonic() >= deadline:
                raise SystemExit('Timed out waiting for the fake visit.')
            sleep(.5)
    finally:
        if pending:
            call('stop', {})
        else:
            call('release', lease)


if __name__ == '__main__':
    main()
