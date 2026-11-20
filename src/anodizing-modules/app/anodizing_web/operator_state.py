"""Stage 1 cache-only projection. All ages come from device acquisition."""
from math import isfinite

from . import agv


def agv_state(sample):
    report, read_at, stamp, connected, mode, recipe, last_station, opened_at, error, now = sample[:10]
    report = report or {}
    limit = 1000 * (agv.RECIPE_STALE_S if recipe else agv.STALE_AFTER)
    age = None if stamp is None else max(0, int(1000 * (now - stamp)))
    source = f'agv.{mode}.status' if mode else 'agv.status'
    station_mode = report.get('mode') == 2 and report.get('max_stations', 0) > 0

    def reported(key, name, value, unit=None):
        reason = ('AGV link lost. Use its e-stop if needed.' if error else 'Connect the AGV.') if not connected else (
            'No status report received.' if stamp is None else
            'Status expired. Check the radio and reconnect.' if age >= limit else
            'The report does not provide this value.' if value is None or
            (isinstance(value, float) and not isfinite(value)) else None)
        result = dict(id=f'agv.{key}', name=name, value=value if reason is None else None, unit=unit,
                      source=source, read_at=read_at, source_at=None,
                      freshness=dict(kind='max_age_ms', max_age_ms=limit),
                      age_ms=age, reason=reason,
                      quality='fresh' if reason is None else 'stale' if connected and age is not None and age >= limit else 'unknown')
        if reason and value is not None and (not isinstance(value, float) or isfinite(value)):
            result['last_read'] = dict(value=value, read_at=read_at)
        return result

    def event(key, name, value, at, unit=None, reason='No active AGV connection.'):
        return dict(id=f'agv.{key}', name=name, value=value, unit=unit, source='agv.connection',
                    read_at=at, source_at=None, freshness=dict(kind='on_event'), age_ms=None,
                    quality='unknown' if value is None else 'fresh', reason=reason if value is None else None)

    stopped = report.get('stopped')
    yield event('connected', 'AGV connected', connected, opened_at)
    yield event('transport', 'Transport', mode if connected else None, opened_at)
    yield event('last_confirmed_station', 'Last confirmed station',
                last_station[0] if connected and last_station else None,
                last_station[1] if connected and last_station else None, 'station',
                'No station arrival has been confirmed on this connection.')
    yield reported('moving', 'Moving', not stopped if type(stopped) is bool else None)
    yield reported('current_station', 'Current station', report.get('current_station') if station_mode else None, 'station')
    yield reported('target_station', 'Target station', report.get('target_station') if station_mode else None, 'station')
    for key, name, field, unit in (
        ('paused', 'Paused', 'paused', None),
        ('speed', 'Actual speed', 'actual_speed_m_h', 'm/h'),
        ('battery', 'Battery', 'battery_percent', '%'),
        ('fault', 'Fault', 'fault_code', None),
        ('obstacle_stop', 'Obstacle stop', 'obstacle_stop', None),
    ):
        yield reported(key, name, report.get(field), unit)


def read(connection):
    sample = connection.state_sample()
    connection_id, sequence = sample[-2:]
    return dict(schema_version=1, generated_at=agv.timestamp(),
                agv=dict(connection_id=connection_id, sequence=sequence),
                variables=sorted(agv_state(sample), key=lambda variable: variable['id']))
