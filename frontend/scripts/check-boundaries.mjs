import ts from 'typescript';
import { readdir, readFile } from 'node:fs/promises';
import { posix, relative, resolve } from 'node:path';

export function violations(path, source) {
  const result = [];
  const file = ts.createSourceFile(
    path,
    source,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TSX,
  );
  const transport = path.startsWith('src/api/');
  const platform = path.startsWith('src/platform/');
  function imported(target) {
    const resolved = target.startsWith('.')
      ? posix.normalize(posix.join(posix.dirname(path), target))
      : target.replace(/^@\//, 'src/').replace(/^\//, '');
    if (resolved.includes('contracts/client-platform') && !transport)
      result.push('wire client outside api');
    if (
      path.startsWith('src/ui/') &&
      /^src\/(?:api|features|platform)(?:\/|$)/.test(resolved)
    )
      result.push('UI primitive imports a feature');
    if (transport && /^src\/(?:features|ui)(?:\/|$)/.test(resolved))
      result.push('protocol imports presentation');
  }
  function visit(node) {
    if (
      (ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) &&
      node.moduleSpecifier &&
      ts.isStringLiteral(node.moduleSpecifier)
    )
      imported(node.moduleSpecifier.text);
    if (
      ts.isCallExpression(node) &&
      (node.expression.kind === ts.SyntaxKind.ImportKeyword ||
        node.expression.getText(file) === 'require') &&
      node.arguments[0] &&
      ts.isStringLiteral(node.arguments[0])
    )
      imported(node.arguments[0].text);
    // Check references as well as calls so aliasing a global cannot bypass ownership.
    const name = ts.isIdentifier(node)
      ? node.text
      : ts.isElementAccessExpression(node) &&
          ts.isStringLiteral(node.argumentExpression) &&
          /^(?:window|globalThis|navigator)$/.test(
            node.expression.getText(file),
          )
        ? node.argumentExpression.text
        : '';
    if (!transport && /^(?:fetch|WebSocket|EventSource)$/.test(name))
      result.push('network outside api');
    if (
      !platform &&
      /^(?:pywebview|showOpenFilePicker|showDirectoryPicker|showSaveFilePicker|clipboard)$/.test(
        name,
      )
    )
      result.push('native/browser capability outside platform');
    ts.forEachChild(node, visit);
  }
  visit(file);
  return result;
}
async function* files(directory) {
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const path = resolve(directory, entry.name);
    if (entry.isDirectory()) yield* files(path);
    else if (/\.tsx?$/.test(path) && !/\.test\.tsx?$/.test(path)) yield path;
  }
}
// Deliberately forbidden behavioral fixtures prove this ratchet detects drift.
const forbidden = [
  ['src/features/fixture.ts', 'fetch("/api/v1")'],
  ['src/ui/fixture.tsx', 'import { x } from "../features/x"'],
  ['src/features/fixture.ts', 'navigator.clipboard.readText()'],
  ['src/ui/fixture.ts', 'import { x } from "../api"'],
  ['src/ui/fixture.ts', 'export { x } from "../platform"'],
  ['src/ui/fixture.ts', 'const x = import("../features/x")'],
  ['src/api/fixture.ts', 'export * from "../ui"'],
  ['src/features/fixture.ts', 'const request = fetch; request("/api/v1")'],
  ['src/features/fixture.ts', 'window["fetch"]("/api/v1")'],
  [
    'src/features/fixture.ts',
    'const { WebSocket: Socket } = window; new Socket("x")',
  ],
  ['src/ui/fixture.ts', 'import { x } from "@/api"'],
  ['src/features/fixture.ts', 'const board = navigator["clipboard"]'],
];
for (const [path, source] of forbidden) {
  if (!violations(path, source).length)
    throw new Error(`Boundary self-test failed: ${path}`);
}
for (const [path, source] of [
  ['src/features/x.ts', 'import { client } from "../../api"'],
  ['src/ui/x.ts', 'import { Button } from "./primitives"'],
  ['src/api/x.ts', 'const request = fetch; request("/api/v1")'],
  ['src/platform/x.ts', 'navigator.clipboard.readText()'],
]) {
  if (violations(path, source).length)
    throw new Error(`Allowed boundary self-test failed: ${path}`);
}
let count = 0;
const errors = [];
for await (const path of files('src')) {
  count++;
  const name = relative(process.cwd(), path).replaceAll('\\', '/');
  for (const message of violations(name, await readFile(path, 'utf8')))
    errors.push(`${name}: ${message}`);
}
console.log(
  `TypeScript import/network boundary: ${count} source files, ${errors.length} violations; ${forbidden.length} negative and 4 allowed fixtures passed`,
);
if (errors.length) {
  console.error(errors.join('\n'));
  process.exitCode = 1;
}
