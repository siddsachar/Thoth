import { spawnSync } from 'node:child_process';
import { readFile, mkdir } from 'node:fs/promises';
import { resolve } from 'node:path';
import { randomUUID } from 'node:crypto';

const output = resolve('../.tmp', `p2-build-${randomUUID().slice(0, 8)}`);
await mkdir(output, { recursive: true });
for (const args of [
  ['node_modules/vite/bin/vite.js', 'build', '--outDir', output],
  ['scripts/asset-manifest.mjs', output],
]) {
  const result = spawnSync(process.execPath, args, {
    stdio: 'inherit',
    env: process.env,
  });
  if (result.status !== 0) process.exit(result.status ?? 1);
}
const inventory = JSON.parse(
  await readFile('dist/asset-manifest.json', 'utf8'),
);
for (const name of [
  ...Object.keys(inventory.files),
  'asset-manifest.json',
  '.vite/manifest.json',
]) {
  const first = await readFile(resolve('dist', name));
  const second = await readFile(resolve(output, name));
  if (!first.equals(second))
    throw new Error(`Production build differs: ${name}`);
}
console.log(
  'Independent production builds are byte-for-byte identical. Both outputs retained.',
);
