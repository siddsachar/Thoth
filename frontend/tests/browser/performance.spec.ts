import { performance } from 'node:perf_hooks';
import {
  test,
  expect,
  distribution,
  writeEvidence,
  assertLocalContentPolicy,
} from './evidence';
import {
  assertConversationMarker,
  openFixture,
  stableConversationMarker,
  type FixtureWindow,
} from './fixture';

// Playwright routing globally disables the HTTP cache, even for a URL predicate.
test.use({ nativeNetwork: true });

test('twenty real-clock panel interactions reach a rendered frame within budget', async ({
  page,
  browser,
}, testInfo) => {
  test.skip(
    testInfo.project.name !== 'chromium-desktop',
    'PB02 calibration uses real desktop Chromium performance.now with no fake clock.',
  );
  await openFixture(page);
  await stableConversationMarker(page);
  const samples: number[] = [];
  for (let sample = 0; sample < 20; sample += 1) {
    await page.getByRole('button', { name: 'Open panel', exact: true }).click();
    const action = page.getByRole('menuitem', {
      name: 'Workspace notes',
      exact: true,
    });
    await action.evaluate((element) => {
      Object.assign(window, { __QA_REAL_PANEL_COMMIT__: null });
      element.addEventListener(
        'click',
        () => {
          const start = performance.now();
          requestAnimationFrame(() =>
            Object.assign(window, {
              __QA_REAL_PANEL_COMMIT__: performance.now() - start,
            }),
          );
        },
        { once: true, capture: true },
      );
    });
    await action.click();
    await expect(
      page.getByRole('heading', { name: 'Workspace notes', exact: true }),
    ).toBeVisible();
    await page.waitForFunction(
      () =>
        typeof (window as unknown as { __QA_REAL_PANEL_COMMIT__: unknown })
          .__QA_REAL_PANEL_COMMIT__ === 'number',
    );
    samples.push(
      await page.evaluate(
        () =>
          (window as unknown as { __QA_REAL_PANEL_COMMIT__: number })
            .__QA_REAL_PANEL_COMMIT__,
      ),
    );
    await page
      .getByRole('button', { name: 'Close all panels', exact: true })
      .click();
    await assertConversationMarker(page);
  }
  await writeEvidence(testInfo, 'PB02-real-clock-feedback', {
    browserVersion: browser.version(),
    samples,
    ...distribution(samples),
    clock:
      'Real performance.now and animation frame; no Playwright clock installed',
    excludedSamples: [],
  });
  expect(distribution(samples).p95).toBeLessThanOrEqual(100);
});

test('cold and warm usable shell samples', async ({
  browser,
  page,
  baseURL,
}, testInfo) => {
  test.skip(
    testInfo.project.name !== 'chromium-desktop',
    'Performance calibration uses one identified desktop Chromium process and controlled fixture; other projects run functional checks.',
  );
  test.setTimeout(120_000);
  const cold: number[] = [];
  for (let sample = 0; sample < 5; sample += 1) {
    const context = await browser.newContext({
      baseURL,
      viewport: { width: 1440, height: 900 },
      locale: 'en-GB',
      colorScheme: 'light',
      serviceWorkers: 'block',
    });
    const errors: string[] = [];
    const fresh = await context.newPage();
    fresh.on('pageerror', (error) => errors.push(error.message));
    fresh.on('console', (event) => {
      if (event.type() === 'error') errors.push(event.text());
    });
    const origin = new URL(baseURL!).origin;
    fresh.on('request', (request) => {
      const url = new URL(request.url());
      if (['http:', 'https:'].includes(url.protocol) && url.origin !== origin)
        errors.push('Unexpected external HTTP request');
    });
    await assertLocalContentPolicy(fresh);
    const started = performance.now();
    await openFixture(fresh);
    await fresh
      .getByRole('button', { name: 'Preferences', exact: true })
      .click();
    await expect(
      fresh.getByRole('dialog', { name: 'Preferences', exact: true }),
    ).toBeVisible();
    await expect(
      fresh.getByRole('combobox', { name: 'Appearance', exact: true }),
    ).toBeVisible();
    cold.push(performance.now() - started);
    expect(errors).toEqual([]);
    await context.close();
  }
  const warm: number[] = [];
  await openFixture(page);
  for (let sample = 0; sample < 10; sample += 1) {
    const started = performance.now();
    await openFixture(page);
    await page
      .getByRole('button', { name: 'Preferences', exact: true })
      .click();
    await expect(
      page.getByRole('dialog', { name: 'Preferences', exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole('combobox', { name: 'Appearance', exact: true }),
    ).toBeVisible();
    warm.push(performance.now() - started);
  }
  await writeEvidence(testInfo, 'PB01-usable-shell', {
    cold: { samples: cold, ...distribution(cold) },
    warm: { samples: warm, ...distribution(warm) },
    method:
      'Cold = five fresh isolated contexts (empty HTTP cache/storage) in same browser process. Warm = ten repeated navigations in one context. Timer spans bootstrap and navigation through actual Preferences interaction and its loaded Appearance control; server readiness is separately attributed by runner.',
    browserVersion: browser.version(),
    fixture: 'F-P01 settled with three synthetic conversation summaries',
    excludedSamples: [],
  });
  expect(distribution(cold).p95).toBeLessThanOrEqual(2000);
  expect(distribution(warm).p95).toBeLessThanOrEqual(1000);
});

test('theme commits and reconnect complete within measured foreground budgets', async ({
  page,
  browser,
}, testInfo) => {
  test.skip(
    testInfo.project.name !== 'chromium-desktop',
    'Twenty-repeat calibration is scoped to identified desktop Chromium.',
  );
  test.setTimeout(120_000);
  await openFixture(page);
  await page
    .getByRole('button', { name: 'A place for your ideas', exact: true })
    .click();
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (window as FixtureWindow).__ROW_BOT_FIXTURE__.transport.counters
            .active,
      ),
    )
    .toBe(1);
  await stableConversationMarker(page);
  await page.getByRole('button', { name: 'Preferences', exact: true }).click();
  const samples: number[] = [];
  for (let sample = 0; sample < 20; sample += 1) {
    await page
      .getByRole('combobox', { name: 'Appearance', exact: true })
      .evaluate((element) => {
        element.addEventListener(
          'change',
          () => {
            const start = performance.now();
            requestAnimationFrame(() =>
              Object.assign(window, {
                __QA_THEME_COMMIT__: performance.now() - start,
              }),
            );
          },
          { once: true },
        );
        Object.assign(window, { __QA_THEME_COMMIT__: null });
      });
    await page
      .getByRole('combobox', { name: 'Appearance', exact: true })
      .selectOption(sample % 2 ? 'light' : 'dark');
    await page.waitForFunction(
      () =>
        typeof (window as unknown as { __QA_THEME_COMMIT__: unknown })
          .__QA_THEME_COMMIT__ === 'number',
    );
    samples.push(
      await page.evaluate(
        () =>
          (window as unknown as { __QA_THEME_COMMIT__: number })
            .__QA_THEME_COMMIT__,
      ),
    );
    await assertConversationMarker(page);
  }
  await page.keyboard.press('Escape');
  const reconnect: number[] = [];
  const expiredReplay: number[] = [];
  for (const expired of [false, true]) {
    for (let sample = 0; sample < 20; sample += 1) {
      await page.evaluate(() =>
        (window as FixtureWindow).__ROW_BOT_FIXTURE__.transport.disconnect(),
      );
      await expect
        .poll(() =>
          page.evaluate(
            () =>
              (
                window as FixtureWindow
              ).__ROW_BOT_FIXTURE__.controller.getSnapshot().status,
          ),
        )
        .toMatch(/^(reconnecting|disconnected)$/);
      await page.evaluate((expire) => {
        const transport = (window as FixtureWindow).__ROW_BOT_FIXTURE__
          .transport;
        if (expire) transport.expireNextReplay();
        transport.scenario = 'normal';
      }, expired);
      await page
        .getByRole('button', { name: 'Reconnect', exact: true })
        .evaluate((element, expire) => {
          element.addEventListener(
            'click',
            () => {
              const start = performance.now();
              const fixture = (window as FixtureWindow).__ROW_BOT_FIXTURE__;
              const controller = fixture.controller;
              const subscriptions = fixture.transport.counters.subscribes;
              const stop = controller.subscribe(() => {
                if (
                  controller.getSnapshot().status === 'ready' &&
                  controller.getSnapshot().projection &&
                  !controller.getSnapshot().loadingConversation &&
                  fixture.transport.counters.subscribes >=
                    subscriptions + (expire ? 2 : 1)
                ) {
                  stop();
                  requestAnimationFrame(() =>
                    Object.assign(window, {
                      __QA_RECONNECT_COMMIT__: performance.now() - start,
                    }),
                  );
                }
              });
            },
            { once: true },
          );
          Object.assign(window, { __QA_RECONNECT_COMMIT__: null });
        }, expired);
      await page
        .getByRole('button', { name: 'Reconnect', exact: true })
        .click();
      await page.waitForFunction(
        () =>
          typeof (window as unknown as { __QA_RECONNECT_COMMIT__: unknown })
            .__QA_RECONNECT_COMMIT__ === 'number',
      );
      (expired ? expiredReplay : reconnect).push(
        await page.evaluate(
          () =>
            (window as unknown as { __QA_RECONNECT_COMMIT__: number })
              .__QA_RECONNECT_COMMIT__,
        ),
      );
      await assertConversationMarker(page);
    }
  }
  const counters = await page.evaluate(() => ({
    transport: (window as FixtureWindow).__ROW_BOT_FIXTURE__.transport.counters,
    selected: (
      window as FixtureWindow
    ).__ROW_BOT_FIXTURE__.controller.getSnapshot().selectedConversationId,
  }));
  expect(counters.selected).toBe('conversation-a');
  expect(counters.transport.commands).toBe(0);
  expect(counters.transport.active).toBe(1);
  expect(counters.transport.streams).toBe(1);
  await writeEvidence(testInfo, 'PB04-PB10-foreground-commits', {
    theme: { samples, ...distribution(samples) },
    reconnect: { samples: reconnect, ...distribution(reconnect) },
    expiredReplay: { samples: expiredReplay, ...distribution(expiredReplay) },
    browserVersion: browser.version(),
    method:
      'Actual select/change or Reconnect click to confirmed state and next animation frame; zero injected RTT; all samples retained.',
    counters,
  });
  expect(distribution(samples).p95).toBeLessThanOrEqual(100);
  expect(distribution(reconnect).p95).toBeLessThanOrEqual(1000);
  expect(distribution(expiredReplay).p95).toBeLessThanOrEqual(1000);
});
