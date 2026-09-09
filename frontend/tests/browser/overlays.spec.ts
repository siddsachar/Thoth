import {
  test,
  expect,
  accessibility,
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

test('workspace command shortcut focuses search and ignores IME composition', async ({
  page,
}, testInfo) => {
  await openFixture(page);
  await stableConversationMarker(page);
  await page.evaluate(() =>
    window.dispatchEvent(
      new KeyboardEvent('keydown', {
        key: 'k',
        ctrlKey: true,
        isComposing: true,
        bubbles: true,
      }),
    ),
  );
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await page.keyboard.press('Control+k');
  await expect(
    page.getByRole('dialog', { name: 'Workspace commands', exact: true }),
  ).toBeVisible();
  const search = page.getByRole('searchbox', {
    name: 'Find a workspace command',
    exact: true,
  });
  await expect(search).toBeFocused();
  await search.fill('appearance');
  await page.keyboard.press('Enter');
  await expect(
    page.getByRole('dialog', { name: 'Preferences', exact: true }),
  ).toBeVisible();
  await expect(page.getByRole('dialog')).toHaveCount(1);
  await assertConversationMarker(page);
  await screenshot(page, testInfo, 'command-to-preferences');
});

test('Preferences traps focus, locks background scroll and restores its opener', async ({
  page,
}, testInfo) => {
  await openFixture(page);
  await stableConversationMarker(page);
  const opener = page.getByRole('button', { name: 'Preferences', exact: true });
  await opener.click();
  const dialog = page.getByRole('dialog', { name: 'Preferences', exact: true });
  await expect(dialog).toBeVisible();
  await expect(page.getByRole('dialog')).toHaveCount(1);
  for (let index = 0; index < 30; index += 1) {
    await page.keyboard.press(index % 2 ? 'Shift+Tab' : 'Tab');
    expect(
      await dialog.evaluate((element) =>
        element.contains(document.activeElement),
      ),
    ).toBe(true);
  }
  expect(
    await page.evaluate(() => getComputedStyle(document.body).overflow),
  ).toBe('hidden');
  await accessibility(page, testInfo, 'preferences-focus-axe');
  await assertNoOverflow(page);
  await screenshot(page, testInfo, 'preferences-focus-scope');
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(opener).toBeFocused();
  await assertConversationMarker(page);
  expect(
    await page.evaluate(() => getComputedStyle(document.body).overflow),
  ).not.toBe('hidden');
});

test('confirmation suspension preserves form draft and Cancel never confirms', async ({
  page,
}, testInfo) => {
  await openFixture(page);
  await stableConversationMarker(page);
  await page.getByRole('button', { name: 'Preferences', exact: true }).click();
  await page
    .getByRole('searchbox', { name: 'Search settings', exact: true })
    .fill('Providers');
  const before = await page.evaluate(() => ({ ...localStorage }));
  await page.getByRole('button', { name: 'Reset layout', exact: true }).click();
  const confirmation = page.getByRole('alertdialog', {
    name: 'Reset layout?',
    exact: true,
  });
  await expect(confirmation).toBeVisible();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.getByRole('alertdialog')).toHaveCount(1);
  await expect(
    confirmation.getByRole('button', { name: 'Cancel', exact: true }),
  ).toBeFocused();
  await screenshot(page, testInfo, 'reset-confirmation-cancel-default');
  await accessibility(page, testInfo, 'confirmation-axe');
  await page.keyboard.press('Escape');
  await expect(page.getByRole('alertdialog')).toHaveCount(0);
  await expect(
    page.getByRole('dialog', { name: 'Preferences', exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole('searchbox', { name: 'Search settings', exact: true }),
  ).toHaveValue('Providers');
  await expect(
    page.getByRole('button', { name: 'Reset layout', exact: true }),
  ).toBeFocused();
  expect(await page.evaluate(() => ({ ...localStorage }))).toEqual(before);
  await page.getByRole('button', { name: 'Reset layout', exact: true }).click();
  await expect(confirmation).toBeVisible();
  await page.mouse.click(2, 2);
  await expect(page.getByRole('alertdialog')).toHaveCount(0);
  await expect(
    page.getByRole('searchbox', { name: 'Search settings', exact: true }),
  ).toHaveValue('Providers');
  expect(await page.evaluate(() => ({ ...localStorage }))).toEqual(before);
  expect(
    await page.evaluate(
      () =>
        (window as FixtureWindow).__ROW_BOT_FIXTURE__.transport.counters
          .commands,
    ),
  ).toBe(0);
  await assertConversationMarker(page);
  await writeEvidence(testInfo, 'cancel-keeps-layout-and-form', {
    unchangedStorage: true,
    retainedQuery: 'Providers',
    commands: 0,
  });
});

test('settings aliases route to explicit current-application placeholders', async ({
  page,
}, testInfo) => {
  await openFixture(page);
  await page.getByRole('button', { name: 'Preferences', exact: true }).click();
  await page
    .getByRole('searchbox', { name: 'Search settings', exact: true })
    .fill('gmail');
  await page.getByRole('link', { name: 'Accounts' }).click();
  await expect(page).toHaveURL(/\/app-v2\/settings\/accounts/);
  await expect(
    page.getByRole('link', { name: 'Open current application', exact: true }),
  ).toHaveAttribute('href', '/');
  await expect(
    page.getByRole('heading', { name: 'Accounts', exact: true }),
  ).toBeVisible();
  await assertNoOverflow(page);
  await screenshot(page, testInfo, 'accounts-setting-placeholder');
});
