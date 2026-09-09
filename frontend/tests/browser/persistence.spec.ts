import { test, expect, screenshot, writeEvidence } from './evidence';
import { openFixture, type FixtureWindow } from './fixture';
import { openPanel, readLayout } from './panel-helpers';

test('an open panel and its instance survive a same-size browser refresh', async ({
  page,
}, testInfo) => {
  await openFixture(page);
  await openPanel(page, 'Activity preview');
  const before = await readLayout(page);
  await page.reload();
  await expect(
    page
      .locator('.sample-panel:visible')
      .getByRole('heading', { name: 'Activity preview', exact: true }),
  ).toBeVisible();
  const after = await readLayout(page);
  expect(after.panels).toEqual(before.panels);
  expect(after.activePanelId).toBe(before.activePanelId);
  expect(
    await page.evaluate(
      () =>
        (window as FixtureWindow).__ROW_BOT_FIXTURE__.transport.counters
          .commands,
    ),
  ).toBe(0);
  await screenshot(page, testInfo, 'panel-restored-after-refresh');
  await writeEvidence(testInfo, 'same-size-panel-persistence', {
    before,
    after,
  });
});

test('compact Back stays on the conversation after refresh while retaining the panel', async ({
  page,
}, testInfo) => {
  test.skip(
    testInfo.project.use.viewport!.width >= 1024,
    'Compact tab Back persistence applies below the desktop breakpoint.',
  );
  await openFixture(page);
  await openPanel(page, 'Activity preview');
  const before = await readLayout(page);
  await page
    .getByRole('button', { name: 'Back to conversation', exact: true })
    .click();
  await expect(page.getByTestId('conversation-placeholder')).toBeVisible();
  await expect
    .poll(async () => (await readLayout(page)).activePanelId)
    .toBeNull();
  await page.reload();
  await expect(page.getByTestId('conversation-placeholder')).toBeVisible();
  await expect(
    page.getByRole('region', { name: 'Compact panel', exact: true }),
  ).toHaveCount(0);
  const after = await readLayout(page);
  expect(after.activePanelId).toBeNull();
  expect(after.panels).toEqual(before.panels);
  await screenshot(page, testInfo, 'compact-back-persisted');
  await writeEvidence(testInfo, 'compact-back-persistence', { before, after });
});

test('version-zero layout migrates, clamps obsolete sizes and resets only after confirmation', async ({
  page,
}, testInfo) => {
  const width = testInfo.project.use.viewport!.width;
  const size = width >= 1024 ? 'desktop' : width >= 768 ? 'tablet' : 'phone';
  await page.addInitScript((size) => {
    const key = `row-bot:layout:v1:local:${size}`;
    if (!localStorage.getItem(key))
      localStorage.setItem(
        key,
        JSON.stringify({
          version: 0,
          navigation: 278,
          side: 99999,
          bottom: 1,
          panels: [
            {
              instance_id: 'panel-7',
              descriptor: {
                panel_kind: 'fake.activity',
                title: 'Migrated activity',
              },
              placement: 'side',
              visibility: 'visible',
            },
          ],
          activePanelId: 'panel-7',
        }),
      );
  }, size);
  await page.goto('/app-v2/?fixture=normal');
  await expect(
    page
      .locator('.sample-panel:visible')
      .getByRole('heading', { name: 'Migrated activity', exact: true }),
  ).toBeVisible();
  const migrated = await readLayout(page);
  expect(migrated.version).toBe(1);
  expect(migrated.navigation.size).toBe(278);
  expect(migrated.side.size).toBeGreaterThanOrEqual(320);
  expect(migrated.side.size).toBeLessThanOrEqual(720);
  expect(migrated.bottom.size).toBe(160);
  expect(migrated.panels[0].instance_id).toBe('panel-7');
  await screenshot(page, testInfo, 'version-zero-layout-migrated');
  await page.getByRole('button', { name: 'Preferences', exact: true }).click();
  await page.getByRole('button', { name: 'Reset layout', exact: true }).click();
  expect((await readLayout(page)).panels).toHaveLength(1);
  await page
    .getByRole('alertdialog', { name: 'Reset layout?', exact: true })
    .getByRole('button', { name: 'Reset layout', exact: true })
    .click();
  await expect.poll(async () => (await readLayout(page)).panels.length).toBe(0);
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('conversation-placeholder')).toBeVisible();
  const reset = await readLayout(page);
  expect(reset.navigation.size).toBe(240);
  expect(
    await page.evaluate(
      () =>
        (window as FixtureWindow).__ROW_BOT_FIXTURE__.transport.counters
          .commands,
    ),
  ).toBe(0);
  await writeEvidence(testInfo, 'migration-and-explicit-reset', {
    migrated,
    reset,
    producerCommands: 0,
  });
});
