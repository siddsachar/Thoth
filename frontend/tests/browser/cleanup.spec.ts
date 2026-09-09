import { test, expect, distribution, writeEvidence } from './evidence';
import {
  assertConversationMarker,
  openFixture,
  stableConversationMarker,
  type FixtureWindow,
} from './fixture';
import { openPanel, readLayout } from './panel-helpers';

test('sixty-second idle and one hundred panel cycles retain no subscriptions, timers or heap', async ({
  page,
  context,
  browser,
}, testInfo) => {
  test.skip(
    testInfo.project.name !== 'chromium-desktop',
    'GC heap calibration requires desktop Chromium CDP; all projects independently cover open/focus/close.',
  );
  test.setTimeout(240_000);
  await page.clock.install();
  await page.addInitScript(() => {
    const timeouts = new Set<number>();
    const intervals = new Set<number>();
    const originalTimeout = window.setTimeout.bind(window);
    const originalInterval = window.setInterval.bind(window);
    const originalClearTimeout = window.clearTimeout.bind(window);
    const originalClearInterval = window.clearInterval.bind(window);
    window.setTimeout = ((
      handler: TimerHandler,
      timeout?: number,
      ...args: unknown[]
    ) => {
      if (typeof handler !== 'function')
        return originalTimeout(handler, timeout, ...args);
      const id = originalTimeout(() => {
        timeouts.delete(id);
        handler(...args);
      }, timeout);
      timeouts.add(id);
      return id;
    }) as typeof window.setTimeout;
    window.setInterval = ((
      handler: TimerHandler,
      timeout?: number,
      ...args: unknown[]
    ) => {
      const id = originalInterval(handler, timeout, ...args);
      intervals.add(id);
      return id;
    }) as typeof window.setInterval;
    window.clearTimeout = ((id?: number) => {
      timeouts.delete(id!);
      intervals.delete(id!);
      originalClearTimeout(id);
    }) as typeof window.clearTimeout;
    window.clearInterval = ((id?: number) => {
      timeouts.delete(id!);
      intervals.delete(id!);
      originalClearInterval(id);
    }) as typeof window.clearInterval;
    Object.assign(window, {
      __QA_TIMER_COUNTS__: () => ({
        timeouts: timeouts.size,
        intervals: intervals.size,
      }),
    });
  });
  await openFixture(page);
  await stableConversationMarker(page);
  await openPanel(page);
  await openPanel(page, 'Activity preview');
  const idleBefore = await page.evaluate(() => ({
    ...(window as FixtureWindow).__ROW_BOT_FIXTURE__.panelMetrics,
  }));
  expect(idleBefore.active).toBe(1);
  await page.evaluate(() =>
    (window as FixtureWindow).__ROW_BOT_FIXTURE__.transport.clock.advance(
      60_000,
    ),
  );
  await page.clock.fastForward(60_000);
  const idleAfter = await page.evaluate(() => ({
    ...(window as FixtureWindow).__ROW_BOT_FIXTURE__.panelMetrics,
  }));
  expect(idleAfter.renders).toBe(idleBefore.renders);
  expect(idleAfter.notifications).toBe(idleBefore.notifications);
  await page
    .getByRole('button', { name: 'Close all panels', exact: true })
    .click();
  await page.clock.fastForward(6500);
  const cdp = await context.newCDPSession(page);
  await cdp.send('HeapProfiler.collectGarbage');
  const heapBefore = await cdp.send('Runtime.getHeapUsage');
  const baseline = await page.evaluate(() => ({
    panels: { ...(window as FixtureWindow).__ROW_BOT_FIXTURE__.panelMetrics },
    timers: (
      window as unknown as {
        __QA_TIMER_COUNTS__: () => { timeouts: number; intervals: number };
      }
    ).__QA_TIMER_COUNTS__(),
  }));
  const cycles: { cycle: number; active: number; references: number }[] = [];
  const feedback: number[] = [];
  await page.evaluate(() => {
    document.addEventListener(
      'click',
      (event) => {
        if (
          !(event.target instanceof Element) ||
          event.target.closest('[role="menuitem"]')?.textContent !==
            'Workspace notes'
        )
          return;
        const started = performance.now();
        Object.assign(window, { __QA_PANEL_COMMIT__: null });
        const observer = new MutationObserver(() => {
          if (
            ![...document.querySelectorAll('.sample-panel h2')].some(
              (element) => element.textContent === 'Workspace notes',
            )
          )
            return;
          observer.disconnect();
          requestAnimationFrame(() =>
            Object.assign(window, {
              __QA_PANEL_COMMIT__: performance.now() - started,
            }),
          );
        });
        observer.observe(document.body, { subtree: true, childList: true });
      },
      true,
    );
  });
  for (let index = 0; index < 100; index += 1) {
    await openPanel(page);
    await page.waitForFunction(
      () =>
        typeof (window as unknown as { __QA_PANEL_COMMIT__: unknown })
          .__QA_PANEL_COMMIT__ === 'number',
    );
    feedback.push(
      await page.evaluate(
        () =>
          (window as unknown as { __QA_PANEL_COMMIT__: number })
            .__QA_PANEL_COMMIT__,
      ),
    );
    await page
      .getByRole('button', { name: 'Close all panels', exact: true })
      .click();
    const state = await page.evaluate(() => ({
      ...(window as FixtureWindow).__ROW_BOT_FIXTURE__.panelMetrics,
    }));
    expect(state.active).toBe(0);
    expect(state.references).toBe(0);
    cycles.push({
      cycle: index + 1,
      active: state.active,
      references: state.references,
    });
  }
  await page.clock.fastForward(6500);
  await cdp.send('HeapProfiler.collectGarbage');
  const heapAfter = await cdp.send('Runtime.getHeapUsage');
  const settled = await page.evaluate(() => ({
    panels: { ...(window as FixtureWindow).__ROW_BOT_FIXTURE__.panelMetrics },
    timers: (
      window as unknown as {
        __QA_TIMER_COUNTS__: () => { timeouts: number; intervals: number };
      }
    ).__QA_TIMER_COUNTS__(),
  }));
  const retainedBytes = heapAfter.usedSize - heapBefore.usedSize;
  await writeEvidence(testInfo, 'PB08-PB09-idle-cleanup-heap', {
    browserVersion: browser.version(),
    idle: {
      durationMs: 60_000,
      clock: 'Playwright clock plus fixture monotonic clock',
      before: idleBefore,
      after: idleAfter,
    },
    cycles,
    feedback: {
      samples: feedback,
      ...distribution(feedback),
      method:
        'Synthetic-clock scheduler audit only; not a real PB02 latency measurement. Real-clock feedback is measured separately.',
    },
    baseline,
    settled,
    heapBefore,
    heapAfter,
    retainedBytes,
    scope:
      'Chromium page JS heap after explicit test-only GC; not browser RSS, server RSS or native process memory',
  });
  expect(settled.panels.active).toBe(0);
  expect(settled.panels.references).toBe(0);
  expect(settled.panels.subscriptions - settled.panels.cleanups).toBe(0);
  expect(settled.timers).toEqual(baseline.timers);
  expect(retainedBytes).toBeLessThanOrEqual(10 * 1024 * 1024);
  expect((await readLayout(page)).panels).toHaveLength(0);
  await assertConversationMarker(page);
});
