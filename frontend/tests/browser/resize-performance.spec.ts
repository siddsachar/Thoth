import { test, expect, distribution, writeEvidence } from './evidence';
import {
  assertConversationMarker,
  openFixture,
  stableConversationMarker,
  type FixtureWindow,
} from './fixture';
import { openPanel } from './panel-helpers';

type ResizeEvidence = {
  active: boolean;
  timer: number;
  events: number;
  feedback: number[];
  frames: number[];
  longTasks: { startTime: number; duration: number }[];
};
type ResizeWindow = FixtureWindow & { __QA_RESIZE__: ResizeEvidence };

test('continuous resize with an active fake stream records desktop and compact frame work', async ({
  page,
  browser,
}, testInfo) => {
  test.skip(
    !['chromium-desktop', 'chromium-tablet'].includes(testInfo.project.name),
    'Named Chromium desktop and touch/tablet calibration; fixed layout functions run across engines.',
  );
  const desktop = testInfo.project.name === 'chromium-desktop';
  await openFixture(page);
  if (!desktop)
    await page
      .getByRole('button', { name: 'Toggle navigation', exact: true })
      .click();
  await page
    .getByRole('button', { name: 'A place for your ideas', exact: true })
    .click();
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (window as FixtureWindow).__ROW_BOT_FIXTURE__.transport.counters
            .streams,
      ),
    )
    .toBe(1);
  await stableConversationMarker(page);
  await openPanel(page, desktop ? 'Workspace notes' : 'Activity preview');
  await page.evaluate(() => {
    const data: ResizeEvidence = {
      active: false,
      timer: 0,
      events: 0,
      feedback: [],
      frames: [],
      longTasks: [],
    };
    let previous = 0;
    new PerformanceObserver((list) => {
      if (data.active)
        data.longTasks.push(
          ...list.getEntries().map((entry) => ({
            startTime: entry.startTime,
            duration: entry.duration,
          })),
        );
    }).observe({ type: 'longtask', buffered: false });
    const interaction = () => {
      if (!data.active) return;
      const start = performance.now();
      requestAnimationFrame(() => {
        if (data.active) data.feedback.push(performance.now() - start);
      });
    };
    document.addEventListener('pointermove', interaction, true);
    window.addEventListener('resize', interaction);
    const frame = (time: number) => {
      if (data.active && previous) data.frames.push(time - previous);
      previous = data.active ? time : 0;
      requestAnimationFrame(frame);
    };
    requestAnimationFrame(frame);
    Object.assign(window, { __QA_RESIZE__: data });
  });
  const begin = async () =>
    page.evaluate(() => {
      const data = (window as ResizeWindow).__QA_RESIZE__;
      data.active = true;
      data.timer = window.setInterval(() => {
        (window as FixtureWindow).__ROW_BOT_FIXTURE__.transport.emitTextDelta();
        data.events += 1;
      }, 25);
    });
  if (desktop) {
    const bounds = await page
      .getByRole('separator', { name: 'Resize side panel', exact: true })
      .boundingBox();
    expect(bounds).not.toBeNull();
    const x = bounds!.x + bounds!.width / 2;
    const y = bounds!.y + Math.min(160, bounds!.height / 2);
    await page.mouse.move(x, y);
    await page.mouse.down();
    await begin();
    await page.mouse.move(x - 140, y, { steps: 60 });
    await page.mouse.move(x + 50, y, { steps: 60 });
    await page.mouse.up();
  } else {
    await begin();
    for (let step = 0; step < 30; step += 1) {
      await page.setViewportSize({
        width: 820 - (step % 6) * 8,
        height: 1180 - (step % 5) * 12,
      });
      await page.evaluate(
        () =>
          new Promise<void>((resolve) =>
            requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
          ),
      );
    }
  }
  const measured = await page.evaluate(() => {
    const data = (window as ResizeWindow).__QA_RESIZE__;
    data.active = false;
    clearInterval(data.timer);
    return {
      feedback: data.feedback,
      frames: data.frames,
      longTasks: data.longTasks,
      events: data.events,
      appliedEvents: (window as FixtureWindow).__ROW_BOT_FIXTURE__.controller
        .metrics.appliedEvents,
    };
  });
  expect(measured.feedback.length).toBeGreaterThanOrEqual(20);
  expect(measured.events).toBeGreaterThan(0);
  expect(measured.appliedEvents).toBeGreaterThan(0);
  await writeEvidence(testInfo, 'PB05-active-stream-resize', {
    browserVersion: browser.version(),
    layout: desktop
      ? 'desktop pointer splitter'
      : 'touch tablet viewport resize',
    samples: measured,
    interactionToFrame: distribution(measured.feedback),
    frameIntervals: distribution(measured.frames),
    budgetMs: desktop ? 16.7 : 33.3,
    method:
      'Real performance.now and rAF; accepted synthetic text deltas every25ms during120 pointer steps or30 compact viewport changes. Report intervals separately; long tasks scoped to active window. This is foundation cache/workspace render load, not a completed chat transcript.',
  });
  await assertConversationMarker(page);
  expect(distribution(measured.feedback).p95).toBeLessThanOrEqual(
    desktop ? 16.7 : 33.3,
  );
  expect(measured.longTasks.filter((task) => task.duration > 100)).toEqual([]);
});
