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

test('system preference is applied before the first frame and updates without remounting', async ({
  page,
}, testInfo) => {
  test.skip(
    ![1440, 390].includes(testInfo.project.use.viewport!.width),
    'System startup matrix uses desktop and phone; blue explicit appearances cover all five sizes.',
  );
  await page.emulateMedia({ colorScheme: 'dark' });
  await page.addInitScript(() => {
    localStorage.setItem(
      'row-bot.appearance.v1',
      JSON.stringify({ version: 1, appearance: 'system', accent: 'blue' }),
    );
    const frames: {
      time: number;
      theme: string | undefined;
      canvas: string;
    }[] = [];
    Object.assign(window, { __QA_STARTUP_FRAMES__: frames });
    function sample(time: number) {
      frames.push({
        time,
        theme: document.documentElement?.dataset.theme,
        canvas:
          document.documentElement?.style.getPropertyValue('--canvas') ?? '',
      });
      if (
        frames.length < 120 &&
        !document.querySelector('[data-testid="conversation-placeholder"]')
      )
        requestAnimationFrame(sample);
    }
    requestAnimationFrame(sample);
  });
  await openFixture(page);
  const frames = await page.evaluate(
    () =>
      (
        window as unknown as {
          __QA_STARTUP_FRAMES__: {
            time: number;
            theme: string;
            canvas: string;
          }[];
        }
      ).__QA_STARTUP_FRAMES__,
  );
  expect(frames.length).toBeGreaterThan(0);
  expect(
    frames.every(
      (frame) => frame.theme === 'dark' && frame.canvas === '#101820',
    ),
  ).toBe(true);
  await writeEvidence(testInfo, 'before-paint-dark-frame-audit', frames);
  await screenshot(page, testInfo, 'system-dark-startup');
  await stableConversationMarker(page);
  for (const mode of ['light', 'dark', 'light'] as const) {
    await page.emulateMedia({ colorScheme: mode });
    await expect(page.locator('html')).toHaveAttribute('data-theme', mode);
    await assertConversationMarker(page);
    await screenshot(page, testInfo, `system-runtime-${mode}`);
  }
});

test('all alternative accents retain readable integrated controls in both appearances', async ({
  page,
}, testInfo) => {
  test.skip(
    ![1440, 390].includes(testInfo.project.use.viewport!.width),
    'Alternative accent matrix is desktop and phone in both appearances.',
  );
  await openFixture(page);
  await stableConversationMarker(page);
  await page.getByRole('button', { name: 'Preferences', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog).toBeVisible();
  for (const appearance of ['light', 'dark']) {
    for (const accent of ['teal', 'violet', 'amber']) {
      await page
        .getByRole('combobox', { name: 'Appearance', exact: true })
        .selectOption(appearance);
      await page
        .getByRole('combobox', { name: 'Colour theme', exact: true })
        .selectOption(accent);
      await expect(page.locator('html')).toHaveAttribute(
        'data-theme',
        appearance,
      );
      await expect(page.locator('html')).toHaveAttribute('data-accent', accent);
      await assertConversationMarker(page);
      await assertNoOverflow(page);
      await accessibility(page, testInfo, `${appearance}-${accent}-axe`);
      await screenshot(page, testInfo, `preferences-${appearance}-${accent}`);
    }
  }
});

test('unavailable or corrupt local storage leaves a usable shell', async ({
  page,
}, testInfo) => {
  const width = testInfo.project.use.viewport!.width;
  const size = width >= 1024 ? 'desktop' : width >= 768 ? 'tablet' : 'phone';
  await page.addInitScript((size) => {
    localStorage.setItem('row-bot.appearance.v1', '{broken');
    localStorage.setItem(
      `row-bot:layout:v1:local:${size}`,
      JSON.stringify({
        version: 999,
        navigation: -999,
        side: 999999,
        panels: [{ instance_id: 'panel-' + '9'.repeat(400) }],
      }),
    );
    Object.defineProperty(Storage.prototype, 'setItem', {
      value: () => {
        throw new DOMException('Storage denied', 'SecurityError');
      },
    });
  }, size);
  await openFixture(page);
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
  await page.getByRole('button', { name: 'Preferences', exact: true }).click();
  await page
    .getByRole('combobox', { name: 'Appearance', exact: true })
    .selectOption('dark');
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await page.keyboard.press('Escape');
  await assertNoOverflow(page);
  expect(
    await page.evaluate(
      () =>
        (window as FixtureWindow).__ROW_BOT_FIXTURE__.transport.counters
          .commands,
    ),
  ).toBe(0);
  await screenshot(page, testInfo, 'storage-denied-still-usable');
});

test('reduced motion, forced colours and narrow 200-percent layout remain operable', async ({
  page,
}, testInfo) => {
  await page.emulateMedia({
    reducedMotion: 'reduce',
    forcedColors: 'active',
    colorScheme: 'dark',
  });
  await page.addInitScript(() => {
    localStorage.setItem(
      'row-bot.appearance.v1',
      JSON.stringify({
        version: 1,
        appearance: 'dark',
        accent: 'blue',
        density: 'compact',
        reduce_transparency: true,
      }),
    );
  });
  await openFixture(page);
  // CSS zoom exercises the actual integrated layout at 200%; browser chrome
  // zoom and physical keyboard/safe-area behaviour remain manual device checks.
  await page.evaluate(() => {
    document.documentElement.style.zoom = '2';
  });
  const headerControls = [];
  for (const name of [
    'Workspace commands',
    'Toggle navigation',
    'Open panel',
    'Preferences',
  ]) {
    headerControls.push(
      await page
        .getByRole('button', { name, exact: true })
        .evaluate((element, label) => {
          const bounds = element.getBoundingClientRect();
          return {
            name: label,
            x: bounds.x,
            y: bounds.y,
            width: bounds.width,
            height: bounds.height,
            right: bounds.right,
            bottom: bounds.bottom,
            viewportWidth: innerWidth,
            viewportHeight: innerHeight,
            hit: element.contains(
              document.elementFromPoint(
                bounds.x + bounds.width / 2,
                bounds.y + bounds.height / 2,
              ),
            ),
          };
        }, name),
    );
  }
  await writeEvidence(
    testInfo,
    '200percent-header-control-geometry',
    headerControls,
  );
  await screenshot(page, testInfo, '200percent-header-before-overlay');
  for (const control of headerControls) {
    expect(control.x, `${control.name} left bound`).toBeGreaterThanOrEqual(0);
    expect(control.right, `${control.name} right bound`).toBeLessThanOrEqual(
      control.viewportWidth + 1,
    );
    expect(control.bottom, `${control.name} bottom bound`).toBeLessThanOrEqual(
      control.viewportHeight + 1,
    );
    expect(control.hit, `${control.name} visible hit target`).toBe(true);
  }
  await page.getByRole('button', { name: 'Preferences', exact: true }).click();
  await expect(page.getByRole('dialog')).toBeVisible();
  const appearance = page.getByRole('combobox', {
    name: 'Appearance',
    exact: true,
  });
  await expect(appearance).toBeVisible();
  await appearance.scrollIntoViewIfNeeded();
  await appearance.selectOption('light');
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
  await appearance.selectOption('dark');
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  const modalControls = [];
  for (const [name, control] of [
    ['Appearance', appearance],
    [
      'Close dialog',
      page.getByRole('button', { name: 'Close dialog', exact: true }),
    ],
    ['Close', page.getByRole('button', { name: 'Close', exact: true })],
  ] as const) {
    await control.scrollIntoViewIfNeeded();
    modalControls.push(
      await control.evaluate((element, label) => {
        const bounds = element.getBoundingClientRect();
        return {
          name: label,
          x: bounds.x,
          y: bounds.y,
          width: bounds.width,
          height: bounds.height,
          right: bounds.right,
          bottom: bounds.bottom,
          viewportWidth: innerWidth,
          viewportHeight: innerHeight,
          hit: element.contains(
            document.elementFromPoint(
              bounds.x + bounds.width / 2,
              bounds.y + bounds.height / 2,
            ),
          ),
        };
      }, name),
    );
  }
  await writeEvidence(
    testInfo,
    '200percent-settled-modal-geometry',
    modalControls,
  );
  await writeEvidence(
    testInfo,
    'emulated-preference-support',
    await page.evaluate(() => ({
      requested: {
        forcedColors: 'active',
        reducedMotion: 'reduce',
        colorScheme: 'dark',
        cssZoom: 2,
      },
      observed: {
        forcedColors: matchMedia('(forced-colors: active)').matches,
        reducedMotion: matchMedia('(prefers-reduced-motion: reduce)').matches,
        dark: matchMedia('(prefers-color-scheme: dark)').matches,
        opaque: document.documentElement.dataset.opaque,
        zoom: getComputedStyle(document.documentElement).zoom,
        background: getComputedStyle(document.querySelector('[role="dialog"]')!)
          .backgroundColor,
        text: getComputedStyle(document.querySelector('[role="dialog"]')!)
          .color,
      },
      limitation:
        'Engine media emulation and CSS zoom only; unsupported forced-colors emulation is reported, not counted as operating-system proof.',
    })),
  );
  await expect(page.locator('html')).toHaveAttribute('data-opaque', 'true');
  await screenshot(
    page,
    testInfo,
    'forced-colours-dark-opaque-reduced-motion-200percent',
  );
  for (const control of modalControls) {
    expect(control.x, `${control.name} left bound`).toBeGreaterThanOrEqual(0);
    expect(control.y, `${control.name} top bound`).toBeGreaterThanOrEqual(0);
    expect(control.right, `${control.name} right bound`).toBeLessThanOrEqual(
      control.viewportWidth + 1,
    );
    expect(control.bottom, `${control.name} bottom bound`).toBeLessThanOrEqual(
      control.viewportHeight + 1,
    );
    expect(control.hit, `${control.name} visible hit target`).toBe(true);
  }
  await assertNoOverflow(page);
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).toHaveCount(0);
});
