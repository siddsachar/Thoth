import {
  test,
  expect,
  accessibility,
  assertNoOverflow,
  screenshot,
  writeEvidence,
} from './evidence';
import { openFixture, type FixtureWindow } from './fixture';

for (const appearance of ['light', 'dark'] as const) {
  test(`shell bootstrap, accessible hierarchy and blue ${appearance}`, async ({
    page,
  }, testInfo) => {
    await page.addInitScript((mode) => {
      localStorage.setItem(
        'row-bot.appearance.v1',
        JSON.stringify({
          version: 1,
          appearance: mode,
          accent: 'blue',
          density: 'comfortable',
          reduce_transparency: false,
        }),
      );
    }, appearance);
    await openFixture(page);
    await expect(page.locator('html')).toHaveAttribute(
      'data-theme',
      appearance,
    );
    await expect(page.locator('html')).toHaveAttribute('data-accent', 'blue');
    await expect(
      page.getByRole('button', { name: 'Preferences', exact: true }),
    ).toBeVisible();
    const proof = await page.evaluate(() => {
      const fixture = (window as FixtureWindow).__ROW_BOT_FIXTURE__;
      return {
        status: fixture.controller.getSnapshot().status,
        commands: fixture.transport.counters.commands,
        stateContainsCsrf:
          'csrf_token' in (fixture.controller.getSnapshot().handshake ?? {}),
        persisted: { ...localStorage },
        session: { ...sessionStorage },
        mountedConversations: document.querySelectorAll(
          '[data-testid="conversation-placeholder"]',
        ).length,
      };
    });
    expect(proof.commands).toBe(0);
    expect(proof.stateContainsCsrf).toBe(false);
    expect(proof.mountedConversations).toBe(1);
    expect(
      JSON.stringify({ persisted: proof.persisted, session: proof.session }),
    ).not.toMatch(/csrf|authorization|session_token|launch_secret/i);
    await writeEvidence(testInfo, 'bootstrap-and-storage', proof);
    await assertNoOverflow(page);
    await accessibility(page, testInfo, `shell-${appearance}-axe`);
    await screenshot(page, testInfo, `shell-blue-${appearance}`);
  });
}

test('forged pywebview or native global cannot enable native authority', async ({
  page,
}, testInfo) => {
  await page.addInitScript(() => {
    Object.assign(window, {
      pywebview: {
        api: {
          native_client_dispatch: () => {
            throw new Error('Forged bridge was invoked');
          },
        },
      },
      __ROW_BOT_NATIVE_CLIENT__: {
        dispatch: () => {
          throw new Error('Forged native endpoint was invoked');
        },
      },
      native_available: true,
    });
  });
  await openFixture(page);
  const result = await page.evaluate(async () => {
    const platform = (window as FixtureWindow).__ROW_BOT_FIXTURE__.platform;
    return {
      discovery: await platform.discover(),
      managedWindow: await platform.managedWindow('/app-v2/'),
    };
  });
  expect(result.discovery).toMatchObject({
    status: 'ok',
    value: { kind: 'browser', platform: 'browser' },
  });
  expect(result.managedWindow).toEqual({
    status: 'unavailable',
    reason: 'managed_windows_require_native',
  });
  await writeEvidence(testInfo, 'forged-native-proof', result);
});

for (const scenario of [
  'incompatible',
  'unauthorized',
  'disconnected',
] as const) {
  test(`${scenario} bootstrap preserves a truthful safe recovery state`, async ({
    page,
  }, testInfo) => {
    await openFixture(page, scenario);
    const state = await page.evaluate(() => {
      const fixture = (window as FixtureWindow).__ROW_BOT_FIXTURE__;
      return {
        snapshot: fixture.controller.getSnapshot(),
        counters: fixture.transport.counters,
      };
    });
    expect(state.snapshot.status).toBe(scenario);
    expect(state.snapshot.error?.message).toBeTruthy();
    await expect(
      page.getByText(state.snapshot.error!.message, { exact: true }),
    ).toBeVisible();
    expect(state.counters.commands).toBe(0);
    expect(state.counters.active).toBe(0);
    if (scenario !== 'disconnected') {
      expect(state.snapshot.handshake).toBeNull();
      expect(state.snapshot.projection).toBeNull();
    }
    await assertNoOverflow(page);
    await accessibility(page, testInfo, `${scenario}-axe`);
    await screenshot(page, testInfo, `${scenario}-recovery`);
    await writeEvidence(testInfo, 'safe-state', state);
  });
}

test('real host bootstrap uses the accepted API and causes no provider work', async ({
  page,
  request,
}, testInfo) => {
  await page.goto('/app-v2/');
  await expect(page.getByTestId('conversation-placeholder')).toBeVisible();
  const compact = testInfo.project.use.viewport!.width < 1024;
  if (compact)
    await page
      .getByRole('button', { name: 'Toggle navigation', exact: true })
      .click();
  await expect(
    page.getByText('Phase 1 conversation A', { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText('Phase 1 conversation B', { exact: true }),
  ).toBeVisible();
  if (compact) await page.keyboard.press('Escape');
  expect(await page.evaluate(() => '__ROW_BOT_FIXTURE__' in window)).toBe(
    false,
  );
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
  await writeEvidence(testInfo, 'real-host-no-provider-calls', state);
  await assertNoOverflow(page);
  await accessibility(page, testInfo, 'real-host-axe');
  await screenshot(page, testInfo, 'real-host-shell');
});

test('real host recovers after browser offline without replaying producer commands', async ({
  page,
  context,
  request,
}, testInfo) => {
  let realEventRequests = 0;
  let acceptedSubscriptions = 0;
  let acknowledgements = 0;
  page.on('request', (request) => {
    if (new URL(request.url()).pathname === '/api/v1/events')
      realEventRequests += 1;
  });
  page.on('response', (response) => {
    if (response.status() !== 200) return;
    const pathname = new URL(response.url()).pathname;
    if (pathname === '/api/v1/conversations/p1-browser-a/subscriptions')
      acceptedSubscriptions += 1;
    if (/^\/api\/v1\/subscriptions\/[^/]+\/ack$/.test(pathname))
      acknowledgements += 1;
  });
  await page.goto('/app-v2/');
  await expect(page.getByTestId('conversation-placeholder')).toBeVisible();
  const compact = testInfo.project.use.viewport!.width < 1024;
  if (compact)
    await page
      .getByRole('button', { name: 'Toggle navigation', exact: true })
      .click();
  await page
    .getByRole('button', { name: 'Phase 1 conversation A', exact: true })
    .click();
  await expect(
    page.getByRole('heading', { name: 'Phase 1 conversation A', exact: true }),
  ).toBeVisible();
  await expect(page.locator('.connection-status')).toHaveText('Connected');
  await expect.poll(() => acceptedSubscriptions).toBe(1);
  await expect.poll(() => acknowledgements).toBe(1);
  await expect.poll(() => realEventRequests).toBe(1);
  expect(await page.evaluate(() => '__ROW_BOT_FIXTURE__' in window)).toBe(
    false,
  );
  await context.setOffline(true);
  try {
    await expect(page.locator('.connection-status')).toHaveText(
      /reconnecting|disconnected/,
      { timeout: 30000 },
    );
    await expect(
      page.getByRole('alert').getByText('Connection interrupted', {
        exact: true,
      }),
    ).toBeVisible();
    await screenshot(page, testInfo, 'real-host-offline');
  } finally {
    await context.setOffline(false);
  }
  await expect(page.locator('.connection-status')).toHaveText('Connected', {
    timeout: 30000,
  });
  await expect.poll(() => acceptedSubscriptions, { timeout: 30000 }).toBe(2);
  await expect.poll(() => acknowledgements, { timeout: 30000 }).toBe(2);
  await expect.poll(() => realEventRequests, { timeout: 30000 }).toBe(2);
  await expect(
    page.getByRole('heading', { name: 'Phase 1 conversation A', exact: true }),
  ).toBeVisible();
  if (compact)
    await page
      .getByRole('button', { name: 'Toggle navigation', exact: true })
      .click();
  await expect(
    page.getByRole('button', { name: 'Phase 1 conversation B', exact: true }),
  ).toBeVisible();
  if (compact) await page.keyboard.press('Escape');
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
  await writeEvidence(testInfo, 'real-host-recovery-no-producer-work', {
    state,
    acceptedSubscriptions,
    acknowledgements,
    realEventRequests,
    method:
      'Real API subscription and acknowledgement responses plus initiated SSE requests before and after browser offline; idle SSE response reporting can wait for the first heartbeat.',
  });
  await screenshot(page, testInfo, 'real-host-recovered');
});
