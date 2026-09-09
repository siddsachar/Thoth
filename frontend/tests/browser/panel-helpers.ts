import type { Page } from '@playwright/test';
import { expect } from './evidence';

export type SavedLayout = {
  version: number;
  panels: {
    instance_id: string;
    descriptor: { title: string };
    placement: string;
  }[];
  activePanelId: string | null;
  navigation: { size: number; collapsed: boolean };
  side: { size: number; collapsed: boolean };
  bottom: { size: number; collapsed: boolean };
};

export async function readLayout(page: Page): Promise<SavedLayout> {
  await page.waitForFunction(() => {
    const size =
      innerWidth >= 1024 ? 'desktop' : innerWidth >= 768 ? 'tablet' : 'phone';
    return Object.keys(localStorage).some(
      (value) =>
        value.startsWith('row-bot:layout:v1:') && value.endsWith(':' + size),
    );
  });
  return page.evaluate(() => {
    const size =
      innerWidth >= 1024 ? 'desktop' : innerWidth >= 768 ? 'tablet' : 'phone';
    const key = Object.keys(localStorage).find(
      (value) =>
        value.startsWith('row-bot:layout:v1:') && value.endsWith(':' + size),
    );
    return JSON.parse(localStorage.getItem(key!)!);
  }) as Promise<SavedLayout>;
}

export async function openPanel(
  page: Page,
  name = 'Workspace notes',
): Promise<void> {
  await page.getByRole('button', { name: 'Open panel', exact: true }).click();
  await page.getByRole('menuitem', { name, exact: true }).click();
  const content = page
    .locator('.sample-panel:visible')
    .getByRole('heading', { name, exact: true });
  await expect(content).toBeVisible();
  await expect
    .poll(
      () =>
        content.evaluate((element) => {
          const bounds = element.getBoundingClientRect();
          return element.contains(
            document.elementFromPoint(
              bounds.x + bounds.width / 2,
              bounds.y + bounds.height / 2,
            ),
          );
        }),
      {
        message:
          'Panel content must be visibly hit-testable, not clipped inside a zero-width dock',
      },
    )
    .toBe(true);
}
