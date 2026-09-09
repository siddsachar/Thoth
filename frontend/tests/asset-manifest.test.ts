import { afterEach, beforeEach, expect, it } from 'vitest';
import { spawnSync } from 'node:child_process';
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmdirSync,
  unlinkSync,
  writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

const script = resolve('scripts/asset-manifest.mjs');
const ownedNames = [
  'index.html',
  'asset-manifest.json',
  '.vite/manifest.json',
  'assets/index-abcdefgh.js',
  'private.txt',
];
let scratch: string;
let build: string;
let stage: string;

beforeEach(() => {
  scratch = mkdtempSync(join(tmpdir(), 'row-bot-client-assets-'));
  build = join(scratch, 'build');
  stage = join(scratch, 'stage');
  mkdirSync(join(build, 'assets'), { recursive: true });
  mkdirSync(join(build, '.vite'));
  writeFileSync(join(build, 'index.html'), '<html>fixture</html>');
  writeFileSync(
    join(build, 'assets/index-abcdefgh.js'),
    'export const fixture = true;',
  );
  writeFileSync(
    join(build, '.vite/manifest.json'),
    JSON.stringify({
      'index.html': { file: 'assets/index-abcdefgh.js', isEntry: true },
    }),
  );
});

afterEach(() => {
  // Remove only known fixture files/directories; never recursively delete output.
  for (const root of [stage, build]) {
    for (const name of ownedNames) {
      const path = join(root, name);
      if (existsSync(path)) unlinkSync(path);
    }
    for (const path of [join(root, 'assets'), join(root, '.vite'), root]) {
      if (existsSync(path)) rmdirSync(path);
    }
  }
  if (existsSync(join(scratch, 'private.txt')))
    unlinkSync(join(scratch, 'private.txt'));
  rmdirSync(scratch);
});

const packageFixture = () =>
  spawnSync(process.execPath, [script, build, '--package-dir', stage], {
    encoding: 'utf8',
    timeout: 5000,
    // Do not inherit credentials, Node preload options or provider settings.
    env: { SystemRoot: process.env.SystemRoot ?? '' },
  });

it('stages exactly the inventoried assets and both private manifests', () => {
  writeFileSync(join(build, 'private.txt'), 'unlisted fixture');
  expect(packageFixture().status).toBe(0);
  for (const name of ownedNames.slice(0, 4)) {
    expect(readFileSync(join(stage, name))).toEqual(
      readFileSync(join(build, name)),
    );
  }
  expect(existsSync(join(stage, 'private.txt'))).toBe(false);
});

it('refuses to merge or delete an existing staging directory', () => {
  mkdirSync(stage);
  writeFileSync(join(stage, 'private.txt'), 'preserved fixture');
  expect(packageFixture().status).toBe(1);
  expect(readFileSync(join(stage, 'private.txt'), 'utf8')).toBe(
    'preserved fixture',
  );
  expect(existsSync(join(stage, 'index.html'))).toBe(false);
});

it('fails a missing source asset before creating staging', () => {
  unlinkSync(join(build, 'assets/index-abcdefgh.js'));
  expect(packageFixture().status).toBe(1);
  expect(existsSync(stage)).toBe(false);
});

it('rejects a traversal manifest before creating staging', () => {
  writeFileSync(join(scratch, 'private.txt'), 'private fixture sentinel');
  writeFileSync(
    join(build, '.vite/manifest.json'),
    JSON.stringify({
      'index.html': { file: '../private.txt', isEntry: true },
    }),
  );
  const result = packageFixture();
  expect(result.status).toBe(1);
  expect(result.stderr).toContain('Invalid generated asset path');
  expect(result.stderr).not.toContain('private fixture sentinel');
  expect(readFileSync(join(scratch, 'private.txt'), 'utf8')).toBe(
    'private fixture sentinel',
  );
  expect(existsSync(stage)).toBe(false);
});
