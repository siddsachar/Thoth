import { createHash } from 'node:crypto';
import { readFile, writeFile, mkdir, copyFile } from 'node:fs/promises';
import { resolve, dirname } from 'node:path';

const root = resolve(process.argv[2] ?? 'dist');
const build = JSON.parse(
  await readFile(resolve(root, '.vite/manifest.json'), 'utf8'),
);
const paths = new Set(['index.html']);
for (const entry of Object.values(build)) {
  paths.add(entry.file);
  for (const file of [...(entry.css ?? []), ...(entry.assets ?? [])])
    paths.add(file);
}
const files = {};
for (const path of [...paths].sort()) {
  if (!/^(index\.html|assets\/[A-Za-z0-9_.-]+)$/.test(path)) {
    throw new Error(`Invalid generated asset path: ${path}`);
  }
  const bytes = await readFile(resolve(root, path));
  files[path] = {
    sha256: createHash('sha256').update(bytes).digest('hex'),
    size: bytes.length,
  };
}
await writeFile(
  resolve(root, 'asset-manifest.json'),
  JSON.stringify({ version: 1, files }, null, 2) + '\n',
);
const packageDirectoryAt = process.argv.indexOf('--package-dir');
if (packageDirectoryAt !== -1 && process.argv.includes('--package')) {
  throw new Error('Choose --package or --package-dir, not both');
}
if (process.argv.includes('--package') || packageDirectoryAt !== -1) {
  const directory = process.argv[packageDirectoryAt + 1];
  if (packageDirectoryAt !== -1 && (!directory || directory.startsWith('--'))) {
    throw new Error('--package-dir requires a fresh destination');
  }
  const destination =
    packageDirectoryAt === -1
      ? resolve('../src/row_bot/static/client-v2')
      : resolve(directory);
  if (packageDirectoryAt !== -1) {
    await mkdir(dirname(destination), { recursive: true });
    // Exclusive fresh staging; never delete or merge a previous installer stage.
    await mkdir(destination);
  }
  for (const path of [...paths, 'asset-manifest.json', '.vite/manifest.json']) {
    await mkdir(dirname(resolve(destination, path)), { recursive: true });
    await copyFile(resolve(root, path), resolve(destination, path));
  }
}
console.log(
  `Verified inventory: ${paths.size} local assets, ${Object.values(files).reduce((sum, f) => sum + f.size, 0)} bytes`,
);
