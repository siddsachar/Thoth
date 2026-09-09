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

test('emulated touch drag resizes a wide tablet dock without replacing the conversation', async ({
  page,
  context,
}, testInfo) => {
  test.skip(
    testInfo.project.name !== 'chromium-tablet',
    'One Chromium CDP emulated touch drag; this is not physical tablet validation.',
  );
  await page.setViewportSize({ width: 1440, height: 900 });
  await openFixture(page);
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
  await openPanel(page);
  const separator = page.getByRole('separator', {
    name: 'Resize side panel',
    exact: true,
  });
  const before = await readLayout(page);
  const bounds = await separator.boundingBox();
  expect(bounds).not.toBeNull();
  const session = await context.newCDPSession(page);
  const x = bounds!.x + bounds!.width / 2;
  const y = bounds!.y + Math.min(100, bounds!.height / 2);
  await session.send('Input.dispatchTouchEvent', {
    type: 'touchStart',
    touchPoints: [{ x, y, id: 1 }],
  });
  for (let step = 1; step <= 20; step++) {
    await session.send('Input.dispatchTouchEvent', {
      type: 'touchMove',
      touchPoints: [{ x: x - step * 4, y, id: 1 }],
    });
  }
  await session.send('Input.dispatchTouchEvent', {
    type: 'touchEnd',
    touchPoints: [],
  });
  await session.detach();
  await expect
    .poll(async () => (await readLayout(page)).side.size)
    .toBeGreaterThan(before.side.size + 40);
  const after = await readLayout(page);
  const protocol = await page.evaluate(() => {
    const { transport, controller } = (window as FixtureWindow)
      .__ROW_BOT_FIXTURE__;
    return {
      counters: transport.counters,
      selected: controller.getSnapshot().selectedConversationId,
    };
  });
  await writeEvidence(testInfo, 'emulated-touch-drag', {
    method:
      'CDP touchStart/touchMove/touchEnd on an emulated touch context; no physical-device claim',
    before,
    after,
    protocol,
  });
  await assertConversationMarker(page);
  await assertNoOverflow(page);
  expect(protocol.selected).toBe('conversation-a');
  expect(protocol.counters.streams).toBe(1);
  expect(protocol.counters.commands).toBe(0);
  await screenshot(page, testInfo, 'emulated-touch-resized-dock');
});
