import { readFileSync, readdirSync } from 'node:fs';
import { parse } from '@vue/compiler-sfc';
import { expect, it } from 'vitest';

const root = new URL('./', import.meta.url);
const files = readdirSync(root, { recursive: true })
  .filter(file => file.endsWith('.vue') && !file.startsWith('node_modules/')).sort();
const idPattern = /^[a-z][a-z0-9]*(\.[a-z][a-z0-9]*)+$/;
const controls = /^(a|button|input|select|textarea|summary|iframe|canvas|form|details|section|article|nav|main|aside|header|Button|OperatorButton|InputText|InputNumber|ToggleSwitch|Select|Checkbox|Textarea|AccordionHeader|Card|Panel|AccordionPanel|ConfirmDialog|DataTable|SnapshotViewer|PickPlaceFields)$/;

function inventory() {
  const missing = [];
  const declarations = [];
  for (const file of files) {
    const source = readFileSync(new URL(file, root), 'utf8');
    const { descriptor } = parse(source);
    function visit(node, repeated = false) {
      if (node.type === 1) {
        const where = `${file}:${node.loc.start.line}`;
        repeated ||= node.props.some(prop => prop.name === 'for' && prop.type === 7);
        const props = node.props;
        const direct = props.find(prop => prop.name === 'data-ui');
        const binds = props.filter(prop => prop.name === 'bind');
        const pt = binds.find(prop => prop.arg?.content === 'pt')?.exp?.content ?? '';
        const literals = [
          ...(direct ? [direct.value?.content ?? ''] : []),
          ...[...pt.matchAll(/'data-ui': '([^']*)'/g)].map(([, id]) => id),
        ];
        const dynamic = binds.find(prop => prop.arg?.content === 'data-ui');
        const forwarded = (file === 'OperatorButton.vue' && node.tag === 'Button') ||
          (file === 'PickPlaceFields.vue' && node.tag === 'InputText');
        const appMenuLink = file === 'layout/AppMenuItem.vue' && node.tag === 'a';
        const interactive = controls.test(node.tag) || (repeated && /^(li|tr)$/.test(node.tag)) || props.some(prop =>
          prop.arg?.content === 'click' || (prop.name === 'tabindex' && prop.value?.content !== '-1'));
        if (interactive && !literals.length && !dynamic) missing.push(where);
        if (dynamic && !forwarded && !appMenuLink) missing.push(`${where} unexpected dynamic ID`);
        const keyed = binds.some(prop => prop.arg?.content === 'data-ui-key') || /'data-ui-key':/.test(pt);
        if (repeated && literals.length && !keyed) missing.push(`${where} needs data-ui-key`);
        declarations.push(...literals.map(id => ({ id, file })));
      }
      node.children?.forEach(child => visit(child, repeated));
    }
    visit(descriptor.template.ast);
    if (file === 'layout/AppMenu.vue') {
      declarations.push(...[...source.matchAll(/ui: '([^']*)'/g)].map(([, id]) => ({ id, file })));
    }
  }
  const entry = readFileSync(new URL('main.js', root), 'utf8');
  declarations.push(...[...entry.matchAll(/'data-ui': '([^']*)'/g)].map(([, id]) => ({ id, file: 'main.js' })));
  return { missing, declarations };
}

it('tags all app-owned controls and panels, with keys inside repeated templates', () => {
  expect(inventory().missing).toEqual([]);
});

it('declares each selector once and keeps the registry in sync with its Vue source', () => {
  const { declarations } = inventory();
  const registry = JSON.parse(readFileSync(new URL('./ui-ids.json', root), 'utf8'));
  const ids = declarations.map(({ id }) => id);
  expect(ids.every(id => idPattern.test(id))).toBe(true);
  expect(ids.filter((id, index) => ids.indexOf(id) !== index)).toEqual([]);
  expect(registry.map(({ id, component }) => ({ id, file: component })))
    .toEqual(declarations.sort((a, b) => a.id.localeCompare(b.id)));
  expect(registry.every(({ description }) => description && !description.includes('\n'))).toBe(true);
});
