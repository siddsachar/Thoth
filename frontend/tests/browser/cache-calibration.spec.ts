import { test, expect, writeEvidence } from './evidence';
import { openFixture, type FixtureWindow } from './fixture';

test('one-thousand and ten-thousand row protocol continuation stays bounded', async ({
  page,
  context,
  browser,
}, testInfo) => {
  test.skip(
    testInfo.project.name !== 'chromium-desktop',
    'Phase2 retained-cache calibration uses explicit Chromium page GC; full transcript rendering/search remains Phase3.',
  );
  await openFixture(page);
  const cdp = await context.newCDPSession(page);
  const workloads: {
    count: number;
    pages: number;
    delivered: number;
    uniqueRowsSeen: number;
    maxRetained: number;
    heapUsed: number;
    mountedTranscriptRows: number;
    placeholderCount: number;
  }[] = [];
  for (const count of [1000, 10000] as const) {
    await page.evaluate(async (size) => {
      const fixture = (window as FixtureWindow).__ROW_BOT_FIXTURE__;
      fixture.transport.setTranscriptSize(size);
      await fixture.controller.selectConversation('conversation-a');
    }, count);
    await expect
      .poll(() =>
        page.evaluate(
          () =>
            (window as FixtureWindow).__ROW_BOT_FIXTURE__.transport.counters
              .streams,
        ),
      )
      .toBe(1);
    const visited = new Set<string>();
    let maxRetained = 0;
    let hasMore = true;
    let steps = 0;
    while (hasMore) {
      const current = await page.evaluate(() => {
        const state = (
          window as FixtureWindow
        ).__ROW_BOT_FIXTURE__.controller.getSnapshot();
        return {
          ids: state.projection?.rows.map((row) => row.id) ?? [],
          hasMore: state.hasMoreTranscript,
          status: state.status,
        };
      });
      expect(current.status).toBe('ready');
      expect(current.ids.length).toBeLessThanOrEqual(200);
      maxRetained = Math.max(maxRetained, current.ids.length);
      current.ids.forEach((id) => visited.add(id));
      hasMore = current.hasMore;
      if (hasMore)
        await page.evaluate(() =>
          (
            window as FixtureWindow
          ).__ROW_BOT_FIXTURE__.controller.loadMoreTranscript(),
        );
      steps += 1;
      expect(steps).toBeLessThanOrEqual(count / 100 + 1);
    }
    await cdp.send('HeapProfiler.collectGarbage');
    const heap = await cdp.send('Runtime.getHeapUsage');
    const result = await page.evaluate(() => ({
      pages: (window as FixtureWindow).__ROW_BOT_FIXTURE__.transport.counters
        .transcriptPages,
      delivered: (window as FixtureWindow).__ROW_BOT_FIXTURE__.transport
        .counters.transcriptRowsDelivered,
      mountedTranscriptRows: document.querySelectorAll('[data-transcript-row]')
        .length,
      placeholderCount: document.querySelectorAll(
        '[data-testid="conversation-placeholder"]',
      ).length,
    }));
    expect(visited.size).toBe(count);
    expect(result.delivered).toBe(count);
    expect(result.pages).toBe(count / 100);
    expect(result.mountedTranscriptRows).toBe(0);
    expect(result.placeholderCount).toBe(1);
    workloads.push({
      count,
      ...result,
      uniqueRowsSeen: visited.size,
      maxRetained,
      heapUsed: heap.usedSize,
    });
  }
  const heapDelta = workloads[1].heapUsed - workloads[0].heapUsed;
  await writeEvidence(testInfo, 'PB07-foundation-cache-calibration', {
    browserVersion: browser.version(),
    workloads,
    heapDelta,
    method:
      'Every100-row continuation visited; synthetic fixture generates each page without holding10k source rows; retained browser heap after test-onlyGC. One unchanged conversation placeholder and0 rendered transcript rows.',
    limitation:
      'This proves bounded protocol cache and cursor reachability only. Anchored10k transcript DOM, full-history search/copy, media/tool-row rendering and PB03 thread-open UX are Phase3 gates.',
  });
  expect(heapDelta).toBeLessThanOrEqual(100 * 1024 * 1024);
});
