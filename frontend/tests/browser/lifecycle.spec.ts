import { test, expect, writeEvidence } from './evidence';
import {
  openFixture,
  stableConversationMarker,
  assertConversationMarker,
  type FixtureWindow,
} from './fixture';

// Routing intercepts all WebKit requests internally, including unload keepalive.
// Actual browser lifecycle checks use the host's strict CSP and observation.
test.use({ nativeNetwork: true });

test('persisted page suspension resumes one observer without disposing the workspace', async ({
  page,
}, testInfo) => {
  await openFixture(page);
  if (testInfo.project.use.viewport!.width < 1024)
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
  await page.evaluate(() =>
    window.dispatchEvent(
      new PageTransitionEvent('pagehide', { persisted: true }),
    ),
  );
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (window as FixtureWindow).__ROW_BOT_FIXTURE__.transport.counters
            .streams,
      ),
    )
    .toBe(0);
  await page.evaluate(() =>
    window.dispatchEvent(
      new PageTransitionEvent('pageshow', { persisted: true }),
    ),
  );
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (window as FixtureWindow).__ROW_BOT_FIXTURE__.transport.counters
            .streams,
      ),
    )
    .toBe(1);
  const result = await page.evaluate(() => {
    const { controller, transport } = (window as FixtureWindow)
      .__ROW_BOT_FIXTURE__;
    return {
      status: controller.getSnapshot().status,
      selected: controller.getSnapshot().selectedConversationId,
      counters: transport.counters,
    };
  });
  expect(result.status).toBe('ready');
  expect(result.selected).toBe('conversation-a');
  expect(result.counters.active).toBe(1);
  expect(result.counters.commands).toBe(0);
  await assertConversationMarker(page);
  await writeEvidence(testInfo, 'persisted-pagehide-pageshow', {
    ...result,
    method:
      'Explicit persisted PageTransitionEvents exercise the page lifecycle contract; actual browser BFCache eligibility is recorded separately.',
  });
});

test('actual browser Back returns to a usable real workspace', async ({
  page,
  request,
}, testInfo) => {
  let acceptedSubscriptions = 0;
  const cleanupResponses: { status: number; path: string }[] = [];
  page.on('response', (response) => {
    const path = new URL(response.url()).pathname;
    if (
      response.request().method() === 'POST' &&
      path.startsWith('/api/v1/conversations/') &&
      path.endsWith('/subscriptions') &&
      response.status() === 200
    )
      acceptedSubscriptions += 1;
    if (
      response.request().method() === 'DELETE' &&
      path.startsWith('/api/v1/subscriptions/')
    )
      cleanupResponses.push({ status: response.status(), path });
  });
  await page.addInitScript(() => {
    Object.assign(window, { __QA_PAGE_SHOW__: [] as boolean[] });
    window.addEventListener('pageshow', (event) => {
      (
        window as Window & typeof globalThis & { __QA_PAGE_SHOW__: boolean[] }
      ).__QA_PAGE_SHOW__.push(event.persisted);
    });
  });
  await page.goto('/app-v2/');
  await expect(page.getByTestId('conversation-placeholder')).toBeVisible();
  const compact = testInfo.project.use.viewport!.width < 1024;
  if (compact)
    await page
      .getByRole('button', { name: 'Toggle navigation', exact: true })
      .click();
  const initialSubscription = page.waitForResponse(
    (response) =>
      response.request().method() === 'POST' &&
      new URL(response.url()).pathname ===
        '/api/v1/conversations/p1-browser-a/subscriptions' &&
      response.status() === 200,
  );
  await page
    .getByRole('button', { name: 'Phase 1 conversation A', exact: true })
    .click();
  await expect(
    page.getByRole('heading', { name: 'Phase 1 conversation A', exact: true }),
  ).toBeVisible();
  await expect.poll(() => acceptedSubscriptions).toBe(1);
  const originalResponse = await initialSubscription;
  const originalHeaders = await originalResponse.request().allHeaders();
  const original = (await originalResponse.json()) as {
    subscription_id: string;
    cursor: string;
  };
  // Synthetic session proof stays in this closure and is never logged or attached.
  const originalProof = {
    'x-client-session': originalHeaders['x-client-session'],
    'x-csrf-token': originalHeaders['x-csrf-token'],
  };
  await page.goto('/readyz');
  await page.goBack();
  await expect(page.getByTestId('conversation-placeholder')).toBeVisible();
  await expect(page.locator('.connection-status')).toHaveText('Connected');
  const pageShows = await page.evaluate(
    () =>
      (window as Window & typeof globalThis & { __QA_PAGE_SHOW__: boolean[] })
        .__QA_PAGE_SHOW__,
  );
  if (pageShows.includes(true)) {
    await expect(
      page.getByRole('heading', {
        name: 'Phase 1 conversation A',
        exact: true,
      }),
    ).toBeVisible();
  }
  if (compact)
    await page
      .getByRole('button', { name: 'Toggle navigation', exact: true })
      .click();
  await expect(
    page.getByRole('button', { name: 'Phase 1 conversation B', exact: true }),
  ).toBeVisible();
  const response = await request.get('/__p1_fixture/state', {
    headers: { 'x-fixture-token': process.env.ROW_BOT_BROWSER_CONTROL_TOKEN! },
  });
  expect(response.ok()).toBe(true);
  const state = (await response.json()) as {
    calls: unknown[];
    external_calls: number;
  };
  expect(state.calls).toEqual([]);
  expect(state.external_calls).toBe(0);
  let cleanupProbe = { status: 0, code: '' };
  await expect
    .poll(
      async () => {
        const reply = await request.get('/api/v1/events/poll', {
          headers: originalProof,
          params: {
            subscription_id: original.subscription_id,
            cursor: original.cursor,
          },
        });
        const body = (await reply.json()) as { code?: string };
        cleanupProbe = { status: reply.status(), code: body.code ?? '' };
        return cleanupProbe;
      },
      {
        message:
          'Old synthetic subscription must be released after actual Back',
      },
    )
    .toEqual({ status: 404, code: 'not_found' });
  expect(cleanupResponses.every((response) => response.status === 200)).toBe(
    true,
  );
  await writeEvidence(testInfo, 'actual-browser-back', {
    pageShows,
    bfcacheUsed: pageShows.includes(true),
    state,
    acceptedSubscriptions,
    cleanupResponses,
    cleanupProbe,
    cleanupProof:
      'The original subscription returns404/not_found under its original privately held session proof after Back, proving server metadata release.',
    cleanupObservation: cleanupResponses.length
      ? 'Observed terminal DELETE responses are HTTP200.'
      : 'No terminal DELETE response reached the old page; unload delivery remains browser-controlled.',
  });
});
