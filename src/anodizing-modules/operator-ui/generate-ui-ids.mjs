import { readFileSync, writeFileSync } from 'node:fs';

const registry = JSON.parse(readFileSync(new URL('./ui-ids.json', import.meta.url), 'utf8'));
const header = [
  '# Operator UI IDs',
  '',
  'Generated from `ui-ids.json` with `node generate-ui-ids.mjs`.',
  '',
  'Open `/operator/?ui=ids` to label the visible controls and panels. Put the query before the route hash, for example `/operator/?ui=ids#/agv`.',
  'Use the ID when requesting a UI change, for example “make `agv.stop` bigger”.',
  'Select one item in a repeated control by its key, for example `[data-ui="agv.call.station"][data-ui-key="2"]`.',
  'Some keys identify records or choices from the current data. Native HTML select choices use the parent select’s ID and the option value.',
  '',
  `${registry.length} registered IDs.`,
  '',
  '| ID | Description | Source |',
  '| --- | --- | --- |',
];
const rows = registry.map(({ id, description, component }) =>
  `| \`${id}\` | ${description} | [${component}](${component}) |`);
const contents = [...header, ...rows, ''].join('\n');
const output = new URL('./UI_IDS.md', import.meta.url);
if (process.argv.includes('--check')) {
  if (readFileSync(output, 'utf8') !== contents) throw Error('Regenerate UI_IDS.md with node generate-ui-ids.mjs.');
} else {
  writeFileSync(output, contents);
}
