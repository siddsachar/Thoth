import {
  test,
  expect,
  accessibility,
  assertNoOverflow,
  screenshot,
} from './evidence';

for (const appearance of ['light', 'dark']) {
  test(`shared primitives retain accessible states in ${appearance}`, async ({
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
    await page.goto('/app-v2/primitives?fixture=normal');
    await expect(
      page.getByRole('heading', { name: 'Component gallery', exact: true }),
    ).toBeVisible();
    await page.getByRole('tab', { name: 'Controls', exact: true }).focus();
    await page.keyboard.press('ArrowRight');
    await expect(
      page.getByRole('tab', { name: 'States', exact: true }),
    ).toHaveAttribute('aria-selected', 'true');
    await expect(
      page.getByRole('button', { name: 'Unavailable', exact: true }),
    ).toBeDisabled();
    await expect(
      page.getByRole('textbox', { name: 'Unavailable input', exact: true }),
    ).toBeDisabled();
    await expect(
      page.getByRole('progressbar', { name: 'Example progress', exact: true }),
    ).toHaveAttribute('value', '65');
    await assertNoOverflow(page);
    await accessibility(page, testInfo, `gallery-${appearance}-axe`);
    await screenshot(page, testInfo, `gallery-${appearance}`);
  });
}

test('modal child menus and popovers stay above the modal; suspension preserves form state', async ({
  page,
}, testInfo) => {
  await page.goto('/app-v2/primitives?fixture=normal');
  await page.getByRole('button', { name: 'Open dialog', exact: true }).click();
  await page
    .getByRole('textbox', { name: 'Example name', exact: true })
    .fill('Preserved local draft');
  await page
    .getByRole('button', { name: 'Example actions', exact: true })
    .click();
  const item = page.getByRole('menuitem', {
    name: 'Keep this idea',
    exact: true,
  });
  await expect(item).toBeVisible();
  expect(
    await item.evaluate((element) => {
      const bounds = element.getBoundingClientRect();
      return element.contains(
        document.elementFromPoint(
          bounds.x + bounds.width / 2,
          bounds.y + bounds.height / 2,
        ),
      );
    }),
  ).toBe(true);
  await item.click();
  await page.getByRole('button', { name: 'Dialog help', exact: true }).click();
  await expect(
    page.getByText('This popover belongs to the active dialog.', {
      exact: true,
    }),
  ).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(
    page.getByRole('dialog', { name: 'Sample dialog', exact: true }),
  ).toBeVisible();
  await page
    .getByRole('button', { name: 'Discard example', exact: true })
    .click();
  await expect(
    page.getByRole('alertdialog', {
      name: 'Discard this example?',
      exact: true,
    }),
  ).toBeVisible();
  await expect(page.locator('[aria-modal="true"]')).toHaveCount(1);
  await page.getByRole('button', { name: 'Cancel', exact: true }).click();
  await expect(
    page.getByRole('textbox', { name: 'Example name', exact: true }),
  ).toHaveValue('Preserved local draft');
  await expect(
    page.getByRole('button', { name: 'Discard example', exact: true }),
  ).toBeFocused();
  await accessibility(page, testInfo, 'restored-form-axe');
  await screenshot(page, testInfo, 'restored-modal-form');
  await page.keyboard.press('Escape');
  await expect(
    page.getByRole('button', { name: 'Open dialog', exact: true }),
  ).toBeFocused();
});

test('sheet footer and command search stay reachable with long content', async ({
  page,
}, testInfo) => {
  await page.goto('/app-v2/primitives?fixture=normal');
  await page
    .getByRole('textbox', { name: 'Find a command', exact: true })
    .fill('notification');
  await expect(
    page.getByRole('button', { name: 'Open sample dialog', exact: true }),
  ).toHaveCount(0);
  await page
    .getByRole('button', { name: 'Show notification', exact: true })
    .click();
  await expect(
    page.getByText('Command completed', { exact: true }),
  ).toBeVisible();
  await page.getByRole('button', { name: 'Open sheet', exact: true }).click();
  await expect(
    page.getByRole('dialog', { name: 'Sample sheet', exact: true }),
  ).toBeVisible();
  const close = page
    .getByRole('dialog')
    .getByRole('button', { name: 'Close', exact: true });
  await expect(close).toBeInViewport();
  expect(
    await close.evaluate((element) => {
      const bounds = element.getBoundingClientRect();
      return element.contains(
        document.elementFromPoint(
          bounds.x + bounds.width / 2,
          bounds.y + bounds.height / 2,
        ),
      );
    }),
    'The modal footer action must remain visible and reachable while a notification is present',
  ).toBe(true);
  await assertNoOverflow(page);
  await screenshot(page, testInfo, 'sheet-long-content-footer');
  await close.click();
  await expect(
    page.getByRole('button', { name: 'Open sheet', exact: true }),
  ).toBeFocused();
});
