import AxeBuilder from '@axe-core/playwright';
import {
  test as base,
  expect,
  type Page,
  type TestInfo,
} from '@playwright/test';
import fs from 'node:fs/promises';

type BrowserEvidence = {
  networkMode: 'external-abort-routing' | 'native-with-verified-csp';
  console: { type: string; text: string }[];
  pageErrors: string[];
  network: { event: string; path: string; status?: number }[];
  blockedExternal: string[];
};

function publicPath(url: string): string {
  try {
    return new URL(url).pathname;
  } catch {
    return '<invalid-url>';
  }
}

function safeText(value: string): string {
  const token = process.env.ROW_BOT_BROWSER_CONTROL_TOKEN;
  return token ? value.split(token).join('<fixture-control>') : value;
}

type ExpectedError = {
  signature: string;
  count: number;
  owner: string;
  fixture: string;
};

function assertConsoleEvidence(
  evidence: BrowserEvidence,
  testInfo: TestInfo,
): void {
  const remaining = evidence.console.filter((entry) => entry.type === 'error');
  for (const annotation of testInfo.annotations.filter(
    (entry) => entry.type === 'expected-console-error',
  )) {
    const expected = JSON.parse(
      annotation.description ?? 'null',
    ) as ExpectedError | null;
    expect(
      expected?.owner,
      'Expected error requires an accountable owner',
    ).toBeTruthy();
    expect(
      expected?.fixture,
      'Expected error requires a named injected fixture',
    ).toBeTruthy();
    expect(
      expected?.signature,
      'Expected errors require an exact observed signature',
    ).toBeTruthy();
    expect(Number.isSafeInteger(expected?.count) && expected!.count > 0).toBe(
      true,
    );
    const matched = remaining.filter(
      (entry) => entry.text === expected!.signature,
    );
    expect(matched).toHaveLength(expected!.count);
    for (const entry of matched) remaining.splice(remaining.indexOf(entry), 1);
  }
  expect(remaining, 'Unexplained console errors').toEqual([]);
}

export async function assertLocalContentPolicy(page: Page): Promise<void> {
  const response = await page.request.get('/app-v2/');
  expect(response.ok()).toBe(true);
  const policy = response.headers()['content-security-policy'];
  expect(policy).toBeTruthy();
  const directives = new Map(
    policy.split(';').map((directive) => {
      const [name, ...sources] = directive.trim().split(/\s+/);
      return [name, sources] as const;
    }),
  );
  for (const directive of [
    'default-src',
    'connect-src',
    'font-src',
    'base-uri',
    'form-action',
  ])
    expect(directives.get(directive), directive).toEqual(["'self'"]);
  expect(
    directives
      .get('script-src')
      ?.every(
        (source) =>
          source === "'self'" || /^'sha256-[A-Za-z0-9+/]+=*'$/.test(source),
      ),
  ).toBe(true);
  expect(directives.get('img-src')).toEqual(["'self'", 'data:', 'blob:']);
  expect(directives.get('style-src')).toEqual(["'self'", "'unsafe-inline'"]);
  expect(directives.get('frame-src')).toEqual(["'none'"]);
  expect(directives.get('object-src')).toEqual(["'none'"]);
}

export const test = base.extend<{
  evidence: BrowserEvidence;
  nativeNetwork: boolean;
}>({
  nativeNetwork: [false, { option: true }],
  evidence: [
    async ({ context, page, baseURL, nativeNetwork }, use, testInfo) => {
      const evidence: BrowserEvidence = {
        networkMode: nativeNetwork
          ? 'native-with-verified-csp'
          : 'external-abort-routing',
        console: [],
        pageErrors: [],
        network: [],
        blockedExternal: [],
      };
      const origin = new URL(baseURL!).origin;
      if (nativeNetwork) await assertLocalContentPolicy(page);
      else
        await context.route(
          (url) =>
            ['http:', 'https:'].includes(url.protocol) && url.origin !== origin,
          async (route) => {
            await route.abort();
          },
        );
      const observe = (observed: Page) => {
        observed.on('request', (request) => {
          const url = new URL(request.url());
          if (
            ['http:', 'https:'].includes(url.protocol) &&
            url.origin !== origin
          )
            evidence.blockedExternal.push(publicPath(url.href));
        });
        observed.on('console', (event) =>
          evidence.console.push({
            type: event.type(),
            text: safeText(event.text()),
          }),
        );
        observed.on('pageerror', (error) =>
          evidence.pageErrors.push(safeText(error.message)),
        );
        observed.on('response', (response) =>
          evidence.network.push({
            event: 'response',
            path: publicPath(response.url()),
            status: response.status(),
          }),
        );
        observed.on('requestfailed', (request) =>
          evidence.network.push({
            event: 'failed',
            path: publicPath(request.url()),
          }),
        );
      };
      observe(page);
      context.on('page', observe);
      await use(evidence);
      await writeEvidence(testInfo, 'browser-observations', evidence);
      expect(
        evidence.pageErrors,
        'Unexplained JavaScript page exceptions',
      ).toEqual([]);
      assertConsoleEvidence(evidence, testInfo);
      expect(
        evidence.blockedExternal,
        'New client must use local assets only',
      ).toEqual([]);
    },
    { auto: true },
  ],
});

export { expect };

export async function writeEvidence(
  testInfo: TestInfo,
  name: string,
  value: unknown,
): Promise<void> {
  const file = testInfo.outputPath(`${name}.json`);
  await fs.mkdir(testInfo.outputDir, { recursive: true });
  await fs.writeFile(file, JSON.stringify(value, null, 2) + '\n');
  await testInfo.attach(name, { path: file, contentType: 'application/json' });
}

export async function screenshot(
  page: Page,
  testInfo: TestInfo,
  name: string,
): Promise<void> {
  const file = testInfo.outputPath(`${name}.png`);
  await page.screenshot({ path: file, fullPage: true, animations: 'disabled' });
  await testInfo.attach(name, { path: file, contentType: 'image/png' });
}

export async function assertNoOverflow(page: Page): Promise<void> {
  const dimensions = await page.evaluate(() => ({
    viewport: document.documentElement.clientWidth,
    content: document.documentElement.scrollWidth,
  }));
  expect(dimensions.content).toBeLessThanOrEqual(dimensions.viewport + 1);
}

export async function accessibility(
  page: Page,
  testInfo: TestInfo,
  name: string,
): Promise<void> {
  const result = await new AxeBuilder({ page })
    .withTags(['wcag2a', 'wcag2aa', 'wcag21aa'])
    .analyze();
  await writeEvidence(testInfo, name, {
    violations: result.violations,
    incomplete: result.incomplete,
    passes: result.passes.length,
  });
  expect(result.violations, 'Integrated automated accessibility scan').toEqual(
    [],
  );
}

export function distribution(samples: number[]): {
  n: number;
  p50: number;
  p95: number;
  max: number;
} {
  const sorted = [...samples].sort((a, b) => a - b);
  const percentile = (p: number) =>
    sorted[Math.max(0, Math.ceil(p * sorted.length) - 1)] ?? 0;
  return {
    n: samples.length,
    p50: percentile(0.5),
    p95: percentile(0.95),
    max: sorted.at(-1) ?? 0,
  };
}
