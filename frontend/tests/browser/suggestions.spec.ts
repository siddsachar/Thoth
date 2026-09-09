import { test, expect, writeEvidence, screenshot } from './evidence';
import {
  openFixture,
  stableConversationMarker,
  assertConversationMarker,
  type FixtureWindow,
} from './fixture';
import { readLayout } from './panel-helpers';

test('an advisory panel waits for explicit open and preserves conversation focus', async ({
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
  if (testInfo.project.use.viewport!.width < 1024) {
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await expect(
      page.getByRole('button', { name: 'Toggle navigation', exact: true }),
    ).toBeFocused();
  }
  const focus = page.getByRole('button', { name: 'Open panel', exact: true });
  await focus.focus();
  await expect(focus).toBeFocused();
  const suggest = () =>
    page.evaluate(() => {
      const controller = (window as FixtureWindow).__ROW_BOT_FIXTURE__
        .controller;
      const conversation = controller.getSnapshot().conversation!;
      controller.suggestPanel({
        type: 'panel.suggested',
        conversation_id: conversation.id,
        conversation_revision: conversation.revision,
        descriptor: { panel_kind: 'fake.info', title: 'QA suggested notes' },
      });
    });
  await suggest();
  await expect(
    page.getByText('Suggested: QA suggested notes', { exact: true }),
  ).toBeVisible();
  expect((await readLayout(page)).panels).toHaveLength(0);
  await expect(focus).toBeFocused();
  await page
    .getByRole('button', { name: 'Dismiss suggestion', exact: true })
    .click();
  await expect(
    page.getByText('Suggested: QA suggested notes', { exact: true }),
  ).toHaveCount(0);
  expect((await readLayout(page)).panels).toHaveLength(0);
  await suggest();
  await page
    .getByRole('button', { name: 'Open suggested panel', exact: true })
    .click();
  await expect(
    page
      .locator('.sample-panel:visible')
      .getByRole('heading', { name: 'QA suggested notes', exact: true }),
  ).toBeVisible();
  expect((await readLayout(page)).panels).toHaveLength(1);
  const result = await page.evaluate(() => {
    const { controller, transport } = (window as FixtureWindow)
      .__ROW_BOT_FIXTURE__;
    return {
      selected: controller.getSnapshot().selectedConversationId,
      suggestions: controller.getSnapshot().suggestions.length,
      commands: transport.counters.commands,
    };
  });
  expect(result).toEqual({
    selected: 'conversation-a',
    suggestions: 0,
    commands: 0,
  });
  await assertConversationMarker(page);
  await screenshot(page, testInfo, 'explicitly-opened-advisory-panel');
  await writeEvidence(testInfo, 'advisory-does-not-steal-focus', {
    ...result,
    source:
      'Explicit client advisory fixture hook; no unknown v1 wire event admitted.',
  });
});
