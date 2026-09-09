import { defineConfig } from '@playwright/test';
import path from 'node:path';

const baseURL = process.env.ROW_BOT_BROWSER_BASE_URL ?? 'http://127.0.0.1:4173';
const origin = new URL(baseURL);
if (!['127.0.0.1', 'localhost', '[::1]'].includes(origin.hostname)) {
  throw new Error(
    'Shell browser fixtures require an isolated loopback backend',
  );
}
const evidence =
  process.env.ROW_BOT_BROWSER_EVIDENCE ??
  path.resolve(
    '../.local/evidence/unified-client-platform/phase-2/qa/playwright-local',
  );
const viewports = [
  { name: 'desktop', width: 1440, height: 900, touch: false },
  { name: 'laptop', width: 1280, height: 720, touch: false },
  { name: 'tablet', width: 820, height: 1180, touch: true },
  { name: 'phone', width: 390, height: 844, touch: true },
  { name: 'narrow', width: 360, height: 800, touch: true },
];
const engines = ['chromium', 'firefox', 'webkit'] as const;
const selectedEngine = process.env.ROW_BOT_BROWSER_ENGINE;
if (selectedEngine && !engines.some((engine) => engine === selectedEngine)) {
  throw new Error('Unknown ROW_BOT_BROWSER_ENGINE');
}

export default defineConfig({
  testDir: './tests/browser',
  outputDir: path.join(evidence, 'artifacts'),
  fullyParallel: false,
  forbidOnly: true,
  retries: 0,
  workers: 1,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: [
    ['list'],
    ['json', { outputFile: path.join(evidence, 'playwright-results.json') }],
  ],
  use: {
    baseURL,
    headless: true,
    serviceWorkers: 'block',
    locale: 'en-GB',
    timezoneId: 'UTC',
    colorScheme: 'light',
    deviceScaleFactor: 1,
    // Raw Playwright traces include authentication headers. Evidence helpers
    // record sanitized event timelines, console messages and screenshots instead.
    trace: 'off',
    video: 'off',
    screenshot: 'only-on-failure',
  },
  projects: engines
    .filter((engine) => !selectedEngine || selectedEngine === engine)
    .flatMap((engine) =>
      viewports.map(({ name, width, height, touch }) => ({
        name: `${engine}-${name}`,
        use: {
          browserName: engine,
          viewport: { width, height },
          hasTouch: touch,
          isMobile: engine === 'firefox' ? false : touch,
          ...(engine === 'chromium' && process.env.ROW_BOT_BROWSER_CHANNEL
            ? { channel: process.env.ROW_BOT_BROWSER_CHANNEL }
            : {}),
          launchOptions:
            engine === 'chromium'
              ? {
                  args: [
                    '--disable-background-networking',
                    '--disable-component-update',
                    '--js-flags=--expose-gc',
                  ],
                }
              : {},
        },
      })),
    ),
});
