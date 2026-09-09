import { test, expect, distribution, writeEvidence } from './evidence';
import {
  openFixture,
  stableConversationMarker,
  assertConversationMarker,
  type FixtureWindow,
} from './fixture';
import { openPanel } from './panel-helpers';

type TraceEvent = {
  name: string;
  cat?: string;
  ph: string;
  ts: number;
  dur?: number;
  pid: number;
  tid: number;
};
type Geometry = {
  width: number;
  viewport: number;
  side: number;
  aria: number | null;
};
type Commit = {
  kind: string;
  latency: number;
  before: Geometry;
  after: Geometry;
};
type WorkWindow = FixtureWindow & {
  __QA_WORK__: {
    phase: 'idle' | 'active' | null;
    frames: { idle: number[]; active: number[] };
    commits: Commit[];
    timer: number;
    events: number;
  };
};

function unionMilliseconds(
  events: TraceEvent[],
  start: number,
  end: number,
): number {
  const ranges = events
    .map((event) => [
      Math.max(start, event.ts),
      Math.min(end, event.ts + (event.dur ?? 0)),
    ])
    .filter(([left, right]) => right > left)
    .sort((a, b) => a[0] - b[0]);
  let total = 0;
  let right = start;
  for (const [left, next] of ranges) {
    total += Math.max(0, next - Math.max(left, right));
    right = Math.max(right, next);
  }
  return total / 1000;
}

test('resize renderer work and settled geometry stay within the named frame budget', async ({
  page,
  context,
  browser,
}, testInfo) => {
  test.skip(
    !['chromium-desktop', 'chromium-tablet'].includes(testInfo.project.name),
    'Named desktop/compact Chromium renderer-main-thread calibration.',
  );
  const desktop = testInfo.project.name === 'chromium-desktop';
  await page.addInitScript(() => {
    const data: WorkWindow['__QA_WORK__'] = {
      phase: null,
      frames: { idle: [], active: [] },
      commits: [],
      timer: 0,
      events: 0,
    };
    Object.assign(window, { __QA_WORK__: data });
    const geometry = (): Geometry => {
      const side = document.querySelector('[aria-label="Side panels"]');
      const separator = document.querySelector(
        '[aria-label="Resize side panel"]',
      );
      return {
        width:
          document.querySelector('.workspace-columns')?.getBoundingClientRect()
            .width ?? 0,
        viewport: innerWidth,
        side: side?.getBoundingClientRect().width ?? 0,
        aria: separator?.getAttribute('aria-valuenow')
          ? Number(separator.getAttribute('aria-valuenow'))
          : null,
      };
    };
    const capture = (event: Event) => {
      if (
        data.phase !== 'active' ||
        (event instanceof PointerEvent && event.buttons !== 1)
      )
        return;
      const started = performance.now();
      const before = geometry();
      requestAnimationFrame(() => {
        if (data.phase !== 'active') return;
        data.commits.push({
          kind: event.type,
          latency: performance.now() - started,
          before,
          after: geometry(),
        });
      });
    };
    window.addEventListener('pointermove', capture, true);
    window.addEventListener('resize', capture, true);
    const frame = (time: number) => {
      if (data.phase) {
        data.frames[data.phase].push(time);
        performance.mark(`row-bot-qa-frame-${data.phase}`);
      }
      requestAnimationFrame(frame);
    };
    requestAnimationFrame(frame);
  });
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
  if (desktop) {
    await page
      .getByRole('separator', { name: 'Resize side panel', exact: true })
      .focus();
    await page.keyboard.press('Home');
  }
  const cdp = await context.newCDPSession(page);
  const trace: TraceEvent[] = [];
  cdp.on('Tracing.dataCollected', (event) =>
    trace.push(...(event.value as unknown as TraceEvent[])),
  );
  await cdp.send('Tracing.start', {
    categories: 'devtools.timeline,toplevel,blink.user_timing',
    options: 'record-as-much-as-possible',
    transferMode: 'ReportEvents',
  });
  await page.evaluate(async () => {
    const data = (window as WorkWindow).__QA_WORK__;
    data.phase = 'idle';
    performance.mark('row-bot-qa-idle-start');
    await new Promise<void>((resolve) => {
      let count = 0;
      const next = () => {
        if (++count >= 61) resolve();
        else requestAnimationFrame(next);
      };
      requestAnimationFrame(next);
    });
    performance.mark('row-bot-qa-idle-end');
    data.phase = null;
  });
  const separator = page.getByRole('separator', {
    name: 'Resize side panel',
    exact: true,
  });
  const bounds = desktop ? await separator.boundingBox() : null;
  if (desktop)
    await page.mouse.move(bounds!.x + bounds!.width / 2, bounds!.y + 100);
  await page.evaluate(() => {
    const data = (window as WorkWindow).__QA_WORK__;
    data.phase = 'active';
    performance.mark('row-bot-qa-active-start');
    data.timer = window.setInterval(() => {
      (window as FixtureWindow).__ROW_BOT_FIXTURE__.transport.emitTextDelta();
      data.events++;
    }, 25);
  });
  if (desktop) {
    const x = bounds!.x + bounds!.width / 2;
    await page.mouse.down();
    await page.mouse.move(x - 140, bounds!.y + 100, { steps: 60 });
    await page.mouse.move(x + 50, bounds!.y + 100, { steps: 60 });
    await page.mouse.up();
  } else {
    for (let step = 1; step <= 30; step++) {
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
  const measured = await page.evaluate(async () => {
    await new Promise<void>((resolve) =>
      requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
    );
    const data = (window as WorkWindow).__QA_WORK__;
    performance.mark('row-bot-qa-active-end');
    data.phase = null;
    clearInterval(data.timer);
    return {
      ...data,
      appliedEvents: (window as FixtureWindow).__ROW_BOT_FIXTURE__.controller
        .metrics.appliedEvents,
    };
  });
  const completed = new Promise<void>((resolve) =>
    cdp.once('Tracing.tracingComplete', () => resolve()),
  );
  await cdp.send('Tracing.end');
  await completed;
  await cdp.detach();
  const activeStart = trace.find(
    (event) => event.name === 'row-bot-qa-active-start',
  );
  expect(
    activeStart,
    'Trace must contain the renderer user timing boundary',
  ).toBeDefined();
  const renderer = trace.filter(
    (event) => event.pid === activeStart!.pid && event.tid === activeStart!.tid,
  );
  const tasks = renderer.filter(
    (event) =>
      event.ph === 'X' &&
      (event.name === 'RunTask' || event.name.endsWith('::RunTask')),
  );
  expect(
    tasks.length,
    'Named renderer top-level tasks must be present',
  ).toBeGreaterThan(0);
  const scripts = renderer.filter(
    (event) =>
      event.ph === 'X' &&
      [
        'FunctionCall',
        'EvaluateScript',
        'EventDispatch',
        'V8.Execute',
      ].includes(event.name),
  );
  const layouts = renderer.filter(
    (event) =>
      event.ph === 'X' && ['Layout', 'UpdateLayoutTree'].includes(event.name),
  );
  const paints = renderer.filter(
    (event) =>
      event.ph === 'X' &&
      ['Paint', 'PrePaint', 'CompositeLayers', 'Layerize'].includes(event.name),
  );
  const phases = (['idle', 'active'] as const).map((phase) => {
    const start = renderer.find(
      (event) => event.name === `row-bot-qa-${phase}-start`,
    )!.ts;
    const end = renderer.find(
      (event) => event.name === `row-bot-qa-${phase}-end`,
    )!.ts;
    const boundaries = [
      start,
      ...renderer
        .filter(
          (event) =>
            event.name === `row-bot-qa-frame-${phase}` &&
            event.ts > start &&
            event.ts < end,
        )
        .map((event) => event.ts)
        .sort((a, b) => a - b),
      end,
    ];
    const frames = boundaries.slice(1).map((right, index) => ({
      start: boundaries[index],
      end: right,
      workMs: unionMilliseconds(tasks, boundaries[index], right),
      scriptMs: unionMilliseconds(scripts, boundaries[index], right),
      layoutMs: unionMilliseconds(layouts, boundaries[index], right),
      paintMs: unionMilliseconds(paints, boundaries[index], right),
    }));
    const timestamps = measured.frames[phase];
    return {
      phase,
      start,
      end,
      frames,
      work: distribution(frames.map((frame) => frame.workMs)),
      cadence: distribution(
        timestamps.slice(1).map((time, index) => time - timestamps[index]),
      ),
      longTasks: tasks.filter(
        (event) =>
          event.ts < end &&
          event.ts + (event.dur ?? 0) > start &&
          (event.dur ?? 0) > 100000,
      ),
    };
  });
  const useful = new Set([
    ...tasks,
    ...scripts,
    ...layouts,
    ...paints,
    ...renderer.filter((event) => event.name.startsWith('row-bot-qa-')),
  ]);
  await writeEvidence(testInfo, 'PB05-renderer-scoped-trace', {
    categories: 'devtools.timeline,toplevel,blink.user_timing',
    renderer: { pid: activeStart!.pid, tid: activeStart!.tid },
    events: [...useful]
      .sort((a, b) => a.ts - b.ts)
      .map(({ name, cat, ph, ts, dur, pid, tid }) => ({
        name,
        cat,
        ph,
        ts,
        dur,
        pid,
        tid,
      })),
    policy:
      'Raw scoped timing events; unrelated processes/events/arguments omitted. No screenshots/netlog/headers/objects. No frame or workload sample excluded.',
  });
  await writeEvidence(testInfo, 'PB05-renderer-work-and-commit', {
    browserVersion: browser.version(),
    layout: desktop ? 'desktop' : 'compact tablet',
    phases,
    commits: measured.commits,
    events: measured.events,
    appliedEvents: measured.appliedEvents,
    budgetMs: desktop ? 16.7 : 33.3,
    excludedSamples: [],
    method:
      'One continuous gesture with120 pointer updates or30 compact viewport changes under25ms synthetic streaming. Renderer-main-thread RunTask union clipped to each user-timing frame/workload boundary; nested intervals never double counted. Script/style/layout/paint subwork reported separately. Idle cadence contextual only, never subtracted. Capture-phase event listener installed before product code verifies actual geometry/ARIA in next rAF.',
  });
  const active = phases.find((phase) => phase.phase === 'active')!;
  expect(active.work.p95).toBeLessThanOrEqual(desktop ? 16.7 : 33.3);
  expect(active.longTasks).toEqual([]);
  expect(measured.commits.length).toBeGreaterThanOrEqual(20);
  for (const commit of measured.commits) {
    expect(commit.after.width).toBeGreaterThan(0);
    expect(
      Math.abs(commit.after.width - commit.after.viewport),
    ).toBeLessThanOrEqual(1);
    if (desktop) {
      expect(commit.after.side).toBeGreaterThanOrEqual(319);
      expect(commit.after.aria).not.toBeNull();
    }
  }
  if (desktop)
    expect(
      measured.commits.filter(
        (commit) => Math.abs(commit.before.side - commit.after.side) > 0.1,
      ).length,
    ).toBeGreaterThanOrEqual(60);
  expect(measured.events).toBeGreaterThan(0);
  expect(measured.appliedEvents).toBeGreaterThan(0);
  await assertConversationMarker(page);
});
