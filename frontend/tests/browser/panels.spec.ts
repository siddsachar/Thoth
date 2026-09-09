import {
  test,
  expect,
  assertNoOverflow,
  screenshot,
  writeEvidence,
} from './evidence';
import {
  assertConversationMarker,
  openFixture,
  stableConversationMarker,
  type FixtureWindow,
} from './fixture';
import { openPanel, readLayout } from './panel-helpers';

test('an open compact navigation drawer updates when a large library page arrives', async ({
  page,
}, testInfo) => {
  await openFixture(page);
  await page.evaluate(async () => {
    const fixture = (window as FixtureWindow).__ROW_BOT_FIXTURE__;
    const template = fixture.transport.conversations[0];
    for (let index = 4; index <= 1005; index += 1)
      fixture.transport.conversations.push({
        ...template,
        id: `library-${index}`,
        title: `Library conversation ${index}`,
      });
    await fixture.controller.loadMoreConversations(true);
  });
  if (testInfo.project.use.viewport!.width < 1024)
    await page
      .getByRole('button', { name: 'Toggle navigation', exact: true })
      .click();
  await page.getByRole('button', { name: 'Show more', exact: true }).click();
  await page
    .getByRole('button', { name: 'Load more conversations', exact: true })
    .click();
  await expect(
    page.getByRole('button', { name: 'Library conversation 100', exact: true }),
  ).toBeAttached();
  await page
    .getByRole('button', { name: 'A place for your ideas', exact: true })
    .click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (window as FixtureWindow).__ROW_BOT_FIXTURE__.controller.getSnapshot()
            .selectedConversationId,
      ),
    )
    .toBe('conversation-a');
  await assertNoOverflow(page);
});

test('compact activity returns to one desktop dock after resize', async ({
  page,
}, testInfo) => {
  test.skip(
    testInfo.project.use.viewport!.width !== 390,
    'One phone→desktop continuity check per browser engine.',
  );
  await openFixture(page);
  await stableConversationMarker(page);
  await openPanel(page, 'Activity preview');
  const instance = (await readLayout(page)).panels[0].instance_id;
  await page.setViewportSize({ width: 1440, height: 900 });
  await expect(page.getByTestId('conversation-placeholder')).toBeVisible();
  await expect(
    page.getByRole('region', { name: 'Compact panel', exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole('heading', { name: 'Activity preview', exact: true }),
  ).toHaveCount(1);
  expect((await readLayout(page)).panels[0].instance_id).toBe(instance);
  await assertConversationMarker(page);
  await screenshot(page, testInfo, 'compact-activity-promoted-to-dock');
});

for (const appearance of ['light', 'dark']) {
  test(`sample panel preserves conversation identity in ${appearance}`, async ({
    page,
  }, testInfo) => {
    await page.addInitScript(
      (mode) =>
        localStorage.setItem(
          'row-bot.appearance.v1',
          JSON.stringify({ version: 1, appearance: mode, accent: 'blue' }),
        ),
      appearance,
    );
    await openFixture(page);
    await stableConversationMarker(page);
    await openPanel(page);
    const first = await readLayout(page);
    expect(first.panels).toHaveLength(1);
    await assertConversationMarker(page);
    await assertNoOverflow(page);
    await screenshot(page, testInfo, `workspace-notes-${appearance}`);
    if (testInfo.project.use.viewport!.width < 1024)
      await page.keyboard.press('Escape');
    await openPanel(page);
    expect((await readLayout(page)).panels).toHaveLength(1);
    if (testInfo.project.use.viewport!.width < 1024)
      await page.keyboard.press('Escape');
    await page
      .getByRole('button', { name: 'Close all panels', exact: true })
      .click();
    expect((await readLayout(page)).panels).toHaveLength(0);
    await assertConversationMarker(page);
    await writeEvidence(testInfo, 'panel-open-focus-close', {
      originalInstance: first.panels[0].instance_id,
      duplicateOpenCreatesNoCopy: true,
      finalPanels: 0,
    });
  });
}

test('dock movement, explicit duplicate and keyboard tab selection', async ({
  page,
}, testInfo) => {
  test.skip(
    testInfo.project.use.viewport!.width < 1024,
    'Desktop dock actions transform into compact sheet/tab navigation at smaller sizes.',
  );
  await openFixture(page);
  await stableConversationMarker(page);
  await openPanel(page);
  const original = (await readLayout(page)).panels[0].instance_id;
  await page
    .getByRole('button', { name: 'Panel actions', exact: true })
    .click();
  await page
    .getByRole('menuitem', { name: 'Move to bottom', exact: true })
    .click();
  expect((await readLayout(page)).panels[0]).toMatchObject({
    instance_id: original,
    placement: 'bottom',
  });
  await expect(
    page.getByRole('region', { name: 'Bottom panels', exact: true }),
  ).toBeVisible();
  await assertConversationMarker(page);
  await screenshot(page, testInfo, 'bottom-panel');
  await page
    .getByRole('button', { name: 'Panel actions', exact: true })
    .click();
  await page
    .getByRole('menuitem', { name: 'Open another copy', exact: true })
    .click();
  const copied = await readLayout(page);
  expect(copied.panels).toHaveLength(2);
  expect(new Set(copied.panels.map((panel) => panel.instance_id)).size).toBe(2);
  const tabs = page
    .getByRole('tablist', { name: 'bottom panel tabs', exact: true })
    .getByRole('tab');
  await tabs.first().focus();
  await page.keyboard.press('ArrowRight');
  await expect(tabs.nth(1)).toBeFocused();
  await expect(tabs.nth(1)).toHaveAttribute('aria-selected', 'true');
  await page.keyboard.press('Home');
  await expect(tabs.first()).toBeFocused();
  await expect(tabs.first()).toHaveAttribute('aria-selected', 'true');
  await assertConversationMarker(page);
  await writeEvidence(testInfo, 'independent-panel-instances', copied);
});

test('keyboard and pointer splitters enforce bounds and collapse/restore', async ({
  page,
}, testInfo) => {
  test.skip(
    testInfo.project.use.viewport!.width < 1024,
    'Compact layouts use a sheet or tab and do not expose desktop splitters.',
  );
  await openFixture(page);
  await stableConversationMarker(page);
  await openPanel(page);
  const navigation = page.getByRole('separator', {
    name: 'Resize navigation',
    exact: true,
  });
  await navigation.focus();
  await page.keyboard.press('Home');
  await expect
    .poll(async () => (await readLayout(page)).navigation.size)
    .toBe(200);
  await page.keyboard.press('Shift+ArrowRight');
  await expect
    .poll(async () => (await readLayout(page)).navigation.size)
    .toBe(248);
  await page.keyboard.press('End');
  await expect
    .poll(async () => (await readLayout(page)).navigation.size)
    .toBe(320);
  await page.keyboard.press('Enter');
  await expect
    .poll(async () => (await readLayout(page)).navigation.collapsed)
    .toBe(true);
  await page
    .getByRole('button', { name: 'Expand navigation', exact: true })
    .click();
  await expect
    .poll(async () => (await readLayout(page)).navigation.collapsed)
    .toBe(false);
  const side = page.getByRole('separator', {
    name: 'Resize side panel',
    exact: true,
  });
  await side.focus();
  await page.keyboard.press('Home');
  await expect.poll(async () => (await readLayout(page)).side.size).toBe(320);
  const box = await side.boundingBox();
  expect(box).not.toBeNull();
  await page.mouse.move(
    box!.x + box!.width / 2,
    box!.y + Math.min(100, box!.height / 2),
  );
  await page.mouse.down();
  await page.mouse.move(box!.x - 160, box!.y + Math.min(100, box!.height / 2), {
    steps: 20,
  });
  await page.mouse.up();
  expect((await readLayout(page)).side.size).toBeGreaterThanOrEqual(320);
  await assertNoOverflow(page);
  await assertConversationMarker(page);
  await screenshot(page, testInfo, 'splitter-bounds-and-pointer');
  await writeEvidence(testInfo, 'final-resized-layout', await readLayout(page));
});

test('responsive transformations retain open resource identity', async ({
  page,
}, testInfo) => {
  test.skip(
    testInfo.project.use.viewport!.width !== 1440,
    'One continuous desktop→tablet→phone→desktop resize per engine; fixed size flows run on all projects.',
  );
  await openFixture(page);
  await stableConversationMarker(page);
  await openPanel(page);
  const original = (await readLayout(page)).panels[0];
  for (const viewport of [
    { width: 820, height: 1180 },
    { width: 390, height: 844 },
    { width: 360, height: 800 },
    { width: 1440, height: 900 },
  ]) {
    await page.setViewportSize(viewport);
    await expect
      .poll(async () =>
        (await readLayout(page)).panels.map((panel) => panel.instance_id),
      )
      .toContain(original.instance_id);
    await assertConversationMarker(page);
    await assertNoOverflow(page);
    await screenshot(page, testInfo, `retained-panel-${viewport.width}`);
  }
});
