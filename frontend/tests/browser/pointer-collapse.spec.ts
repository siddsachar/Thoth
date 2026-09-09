import { test, expect, writeEvidence } from './evidence';
import {
  openFixture,
  stableConversationMarker,
  assertConversationMarker,
} from './fixture';
import { openPanel, readLayout } from './panel-helpers';

for (const region of ['navigation', 'side', 'bottom'] as const) {
  test(`pointer collapse of ${region} persists and restores the same resource`, async ({
    page,
  }, testInfo) => {
    test.skip(
      testInfo.project.use.viewport!.width !== 1440,
      'All three pointer-collapse paths run once per engine at desktop size.',
    );
    await openFixture(page);
    await stableConversationMarker(page);
    if (region !== 'navigation') await openPanel(page);
    if (region === 'bottom') {
      await page
        .getByRole('button', { name: 'Panel actions', exact: true })
        .click();
      await page
        .getByRole('menuitem', { name: 'Move to bottom', exact: true })
        .click();
    }
    const original = await readLayout(page);
    const name =
      region === 'navigation' ? 'Resize navigation' : `Resize ${region} panel`;
    const separator = page.getByRole('separator', { name, exact: true });
    await expect(separator).toBeVisible();
    const box = (await separator.boundingBox())!;
    const viewport = page.viewportSize()!;
    const x = box.x + Math.min(box.width / 2, 100);
    const y = box.y + Math.min(box.height / 2, 100);
    await page.mouse.move(x, y);
    await page.mouse.down();
    await page.mouse.move(
      region === 'navigation' ? 1 : region === 'side' ? viewport.width - 1 : x,
      region === 'bottom' ? viewport.height - 1 : y,
      { steps: 30 },
    );
    await page.mouse.up();
    await page.evaluate(
      () =>
        new Promise<void>((resolve) =>
          requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
        ),
    );
    await writeEvidence(testInfo, `pointer-${region}-collapse-observed`, {
      original,
      after: await readLayout(page),
      geometry: await page
        .locator(
          region === 'navigation' ? '#navigation-pane' : `#${region}-pane`,
        )
        .evaluate((element) => {
          const bounds = element.getBoundingClientRect();
          return { width: bounds.width, height: bounds.height };
        }),
    });
    await expect
      .poll(async () => (await readLayout(page))[region].collapsed)
      .toBe(true);
    await assertConversationMarker(page);
    await page.reload();
    await expect
      .poll(async () => (await readLayout(page))[region].collapsed)
      .toBe(true);
    expect((await readLayout(page)).panels).toEqual(original.panels);
    if (region === 'navigation') {
      await page
        .getByRole('button', { name: 'Expand navigation', exact: true })
        .click();
    } else {
      await page
        .getByRole('complementary', { name: 'Panel rail', exact: true })
        .getByRole('button', { name: 'Workspace notes', exact: true })
        .click();
      await expect(
        page
          .locator('.sample-panel:visible')
          .getByRole('heading', { name: 'Workspace notes', exact: true }),
      ).toBeVisible();
    }
    await expect
      .poll(async () => (await readLayout(page))[region].collapsed)
      .toBe(false);
    expect((await readLayout(page)).panels).toEqual(original.panels);
    await expect(
      page.getByRole('separator', { name, exact: true }),
    ).toBeVisible();
  });
}
