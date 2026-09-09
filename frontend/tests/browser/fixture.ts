import type { Page } from '@playwright/test';
import type { ClientController } from '../../src/api/controller';
import type { FixtureTransport } from '../../src/api/fixtures';
import type { ClientPlatform } from '../../src/platform/types';
import { expect } from './evidence';

export type FixtureWindow = Window &
  typeof globalThis & {
    __ROW_BOT_FIXTURE__: {
      controller: ClientController;
      transport: FixtureTransport;
      platform: ClientPlatform;
      panelMetrics: {
        subscriptions: number;
        cleanups: number;
        notifications: number;
        active: number;
        references: number;
        renders: number;
      };
    };
  };

export async function openFixture(
  page: Page,
  scenario = 'normal',
): Promise<void> {
  await page.goto(`/app-v2/?fixture=${scenario}`);
  await page.waitForFunction(
    () => !!(window as FixtureWindow).__ROW_BOT_FIXTURE__,
  );
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (window as FixtureWindow).__ROW_BOT_FIXTURE__.controller.getSnapshot()
            .status,
      ),
    )
    .not.toBe('loading');
  if (scenario === 'normal') {
    await expect(page.getByTestId('conversation-placeholder')).toBeVisible();
    await expect
      .poll(() =>
        page.evaluate(
          () =>
            (
              window as FixtureWindow
            ).__ROW_BOT_FIXTURE__.controller.getSnapshot().status,
        ),
      )
      .toBe('ready');
  }
}

export async function stableConversationMarker(page: Page): Promise<void> {
  await page.getByTestId('conversation-placeholder').evaluate((element) => {
    element.setAttribute('data-qa-identity', 'retained-node');
  });
}

export async function assertConversationMarker(page: Page): Promise<void> {
  await expect(page.getByTestId('conversation-placeholder')).toHaveAttribute(
    'data-qa-identity',
    'retained-node',
  );
}
