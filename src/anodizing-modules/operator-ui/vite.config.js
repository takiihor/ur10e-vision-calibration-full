import { readdirSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import vue from '@vitejs/plugin-vue';
import { defineConfig } from 'vite';

const output = fileURLToPath(new URL('../app/anodizing_web/operator_dist/', import.meta.url));
export default defineConfig({
  base: '/operator/',
  plugins: [
    vue(),
    {
      name: 'operator-asset-allowlist',
      closeBundle() {
        const assets = readdirSync(output + 'assets').sort().map(name => 'assets/' + name);
        writeFileSync(output + 'asset-allowlist.json', JSON.stringify(assets, null, 2) + '\n');
      },
    },
  ],
  build: { outDir: output, emptyOutDir: true, assetsInlineLimit: 0 },
});
