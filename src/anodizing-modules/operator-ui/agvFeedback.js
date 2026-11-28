import { localTime, stationLimit } from '../app/anodizing_web/web/agv-record.mjs';

export const stationUnknown = 'Station count unknown: on the AGV page choose Wired, Connect, then Query status.';
export const teachStations = 'Teach the missing stations using the XBOT station-learning procedure, then press Query status.';

export function learnedRange(raw) {
  const maximum = stationLimit({ max_stations: raw });
  if (maximum === undefined) return '';
  return `The AGV reports ${raw} learned ${raw === 1 ? 'leg' : 'legs'}: ${maximum ? `stations 1 to ${maximum}` : 'no callable stations'}.`;
}

export function stationReport(report) {
  const range = learnedRange(report?.value);
  if (!range) return stationUnknown;
  const position = report.current_station == null ? 'The current station is unknown' :
    `It ${report.stale ? 'was' : 'is'} at station ${report.current_station}`;
  const readTime = report.observed_at ? ` (read ${localTime(report.observed_at).split('.')[0]})` : '';
  const stale = report.stale ? ' Last known. Press Query status for a new reading.' : '';
  return `${range} ${position}${readTime}.${stale}`;
}

export function guidance(feedback, raw = '') {
  return feedback ? [feedback.message, feedback.hint].filter(Boolean).join(' ') : raw;
}

export function refused(action, error, fallback) {
  const reason = error.operatorMessage ?
    guidance({ message: error.operatorMessage, hint: error.hint }) :
    error.status ? `Reason reported: ${(error.code || error.message).replace(/\.$/, '')}. Tell the technician.` : fallback;
  return `${action} refused. ${reason}`;
}

export function ownerLabel(name) {
  return name === 'agv-page' ? 'AGV page' : name === 'calibration' ? 'Calibration module' : name;
}
