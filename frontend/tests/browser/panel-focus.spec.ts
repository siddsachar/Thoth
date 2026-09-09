import { test, expect, writeEvidence } from './evidence';
import {
  openFixture,
  assertConversationMarker,
  stableConversationMarker,
} from './fixture';
import { openPanel, readLayout } from './panel-helpers';

test('closing a compact notes sheet returns focus to the connected Open panel trigger', async ({
  page,
}, testInfo) => {
  test.skip(
    testInfo.project.use.viewport!.width >= 1024,
    'Notes use a sheet below the desktop breakpoint.',
  );
  await openFixture(page);
  await stableConversationMarker(page);
  await openPanel(page);
  await page
    .getByRole('dialog', { name: 'Workspace notes', exact: true })
    .getByRole('button', { name: 'Close', exact: true })
    .click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(
    page.getByRole('button', { name: 'Open panel', exact: true }),
  ).toBeFocused();
  await assertConversationMarker(page);
  await writeEvidence(testInfo, 'compact-sheet-focus-return', {
    trigger: 'Open panel',
    connected: true,
  });
});

test('closing the final desktop panel or all panels returns focus to Open panel', async ({
  page,
}, testInfo) => {
  test.skip(
    testInfo.project.use.viewport!.width < 1024,
    'Desktop dock actions have separate compact sheet/tab navigation.',
  );
  await openFixture(page);
  await stableConversationMarker(page);
  await openPanel(page);
  await page
    .getByRole('button', { name: 'Panel actions', exact: true })
    .click();
  await page
    .getByRole('menuitem', { name: 'Close panel', exact: true })
    .click();
  await expect.poll(async () => (await readLayout(page)).panels.length).toBe(0);
  await expect(
    page.getByRole('button', { name: 'Open panel', exact: true }),
  ).toBeFocused();
  await openPanel(page);
  await openPanel(page, 'Activity preview');
  expect((await readLayout(page)).panels).toHaveLength(2);
  await page
    .getByRole('button', { name: 'Close all panels', exact: true })
    .click();
  await expect.poll(async () => (await readLayout(page)).panels.length).toBe(0);
  await expect(
    page.getByRole('button', { name: 'Open panel', exact: true }),
  ).toBeFocused();
  await assertConversationMarker(page);
  await writeEvidence(testInfo, 'desktop-panel-close-focus-return', {
    finalPanel: 'Open panel',
    allPanels: 'Open panel',
  });
});

test('moving a focused dock follows its panel and collapsing returns focus to Open panel', async ({
  page,
}, testInfo) => {
  test.skip(
    testInfo.project.use.viewport!.width < 1024,
    'Desktop move and collapse focus use dock tabs.',
  );
  await openFixture(page);
  await stableConversationMarker(page);
  await openPanel(page);
  await page
    .getByRole('button', { name: 'Panel actions', exact: true })
    .click();
  await page
    .getByRole('menuitem', { name: 'Move to bottom', exact: true })
    .click();
  const bottom = page.getByRole('region', {
    name: 'Bottom panels',
    exact: true,
  });
  await expect(
    bottom.getByRole('tab', { name: 'Workspace notes', exact: true }),
  ).toBeFocused();
  await bottom
    .getByRole('button', { name: 'Panel actions', exact: true })
    .click();
  await page
    .getByRole('menuitem', { name: 'Collapse panel', exact: true })
    .click();
  await expect(
    page.getByRole('button', { name: 'Open panel', exact: true }),
  ).toBeFocused();
  expect((await readLayout(page)).bottom.collapsed).toBe(true);
  await assertConversationMarker(page);
  await writeEvidence(testInfo, 'move-collapse-focus-continuity', {
    moved: 'Workspace notes tab in Bottom panels',
    collapsed: 'Open panel',
  });
});

test('a command opened on desktop uses the current compact layout after resize', async ({
  page,
}, testInfo) => {
  test.skip(
    testInfo.project.use.viewport!.width !== 1440,
    'One live desktop-to-phone command transition per engine.',
  );
  await openFixture(page);
  await stableConversationMarker(page);
  await page
    .getByRole('button', { name: 'Workspace commands', exact: true })
    .click();
  await expect(
    page.getByRole('dialog', { name: 'Workspace commands', exact: true }),
  ).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await page
    .getByRole('searchbox', { name: 'Find a workspace command', exact: true })
    .fill('Workspace notes');
  await page
    .getByRole('button', { name: 'Open Workspace notes', exact: true })
    .click();
  await expect(
    page.getByRole('dialog', { name: 'Workspace notes', exact: true }),
  ).toBeVisible();
  await expect(
    page
      .locator('.sample-panel:visible')
      .getByRole('heading', { name: 'Workspace notes', exact: true }),
  ).toBeVisible();
  const layout = await readLayout(page);
  expect(layout.panels).toHaveLength(1);
  await expect(
    page.getByRole('region', { name: 'Side panels', exact: true }),
  ).toHaveCount(0);
  await assertConversationMarker(page);
  await writeEvidence(testInfo, 'command-uses-latest-compact-layout', layout);
});
