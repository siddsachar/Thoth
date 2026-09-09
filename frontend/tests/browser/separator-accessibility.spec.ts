import type { Page } from '@playwright/test';
import { test, expect, writeEvidence, accessibility } from './evidence';
import {
  openFixture,
  stableConversationMarker,
  assertConversationMarker,
} from './fixture';
import { openPanel } from './panel-helpers';

async function range(page: Page, name: string) {
  const separator = page.getByRole('separator', { name, exact: true });
  await expect(separator).toBeVisible();
  await expect
    .poll(
      () =>
        separator.evaluate((element) =>
          ['aria-valuemin', 'aria-valuemax', 'aria-valuenow'].every(
            (attribute) => {
              const value = element.getAttribute(attribute);
              return (
                value !== null && value !== '' && Number.isFinite(Number(value))
              );
            },
          ),
        ),
      { message: `${name} must expose its numeric accessible range` },
    )
    .toBe(true);
  const result = await separator.evaluate((element) => ({
    name: element.getAttribute('aria-label'),
    controls: element.getAttribute('aria-controls'),
    targetExists: !!document.getElementById(
      element.getAttribute('aria-controls') ?? '',
    ),
    min: Number(element.getAttribute('aria-valuemin')),
    max: Number(element.getAttribute('aria-valuemax')),
    now: Number(element.getAttribute('aria-valuenow')),
    orientation: element.getAttribute('aria-orientation'),
  }));
  expect(result.controls).toBeTruthy();
  expect(result.targetExists).toBe(true);
  expect(result.now).toBeGreaterThanOrEqual(result.min);
  expect(result.now).toBeLessThanOrEqual(result.max);
  return result;
}

test('all three separators expose live ranges and controls through registration changes', async ({
  page,
}, testInfo) => {
  test.skip(
    testInfo.project.use.viewport!.width < 1024,
    'Desktop splitter semantics run at both desktop widths in every engine; compact layouts intentionally omit splitters.',
  );
  await openFixture(page);
  await stableConversationMarker(page);
  const snapshots = [await range(page, 'Resize navigation')];
  const navigation = page.getByRole('separator', {
    name: 'Resize navigation',
    exact: true,
  });
  await navigation.focus();
  await page.keyboard.press('Home');
  const navigationSmall = await range(page, 'Resize navigation');
  await page.keyboard.press('ArrowRight');
  await expect
    .poll(async () => (await range(page, 'Resize navigation')).now)
    .not.toBe(navigationSmall.now);
  await page.keyboard.press('Enter');
  await page
    .getByRole('button', { name: 'Expand navigation', exact: true })
    .click();
  snapshots.push(await range(page, 'Resize navigation'));
  await openPanel(page);
  const side = page.getByRole('separator', {
    name: 'Resize side panel',
    exact: true,
  });
  snapshots.push(await range(page, 'Resize side panel'));
  await side.focus();
  await page.keyboard.press('Home');
  const sideSmall = await range(page, 'Resize side panel');
  await page.keyboard.press('ArrowLeft');
  await expect
    .poll(async () => (await range(page, 'Resize side panel')).now)
    .not.toBe(sideSmall.now);
  const beforeDrag = await range(page, 'Resize side panel');
  const bounds = await side.boundingBox();
  await page.mouse.move(bounds!.x + bounds!.width / 2, bounds!.y + 100);
  await page.mouse.down();
  await page.mouse.move(bounds!.x - 70, bounds!.y + 100, { steps: 12 });
  await page.mouse.up();
  await expect
    .poll(async () => (await range(page, 'Resize side panel')).now)
    .not.toBe(beforeDrag.now);
  snapshots.push(await range(page, 'Resize side panel'));
  await page
    .getByRole('button', { name: 'Panel actions', exact: true })
    .click();
  await page
    .getByRole('menuitem', { name: 'Move to bottom', exact: true })
    .click();
  await expect(page.getByRole('menu')).toHaveCount(0);
  await expect(
    page
      .getByRole('region', { name: 'Bottom panels', exact: true })
      .getByRole('tab', { name: 'Workspace notes', exact: true }),
  ).toBeFocused();
  await expect(side).toHaveCount(0);
  const bottom = page.getByRole('separator', {
    name: 'Resize bottom panel',
    exact: true,
  });
  snapshots.push(await range(page, 'Resize bottom panel'));
  await bottom.focus();
  await expect(bottom).toBeFocused();
  await page.keyboard.press('Home');
  const bottomSmall = await range(page, 'Resize bottom panel');
  await expect(bottom).toBeFocused();
  await page.keyboard.press('ArrowUp');
  await expect
    .poll(async () => (await range(page, 'Resize bottom panel')).now)
    .not.toBe(bottomSmall.now);
  await expect(bottom).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(bottom).toHaveCount(0);
  await expect(
    page.getByRole('button', { name: 'Open panel', exact: true }),
  ).toBeFocused();
  await page
    .getByRole('complementary', { name: 'Panel rail', exact: true })
    .getByRole('button', { name: 'Workspace notes', exact: true })
    .click();
  snapshots.push(await range(page, 'Resize bottom panel'));
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole('separator')).toHaveCount(0);
  await page.setViewportSize(testInfo.project.use.viewport!);
  snapshots.push(await range(page, 'Resize navigation'));
  snapshots.push(await range(page, 'Resize bottom panel'));
  await accessibility(page, testInfo, 'registered-separator-axe');
  await assertConversationMarker(page);
  await writeEvidence(testInfo, 'registered-separator-ranges', snapshots);
});
