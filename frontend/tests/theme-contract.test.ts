import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  bootstrapTheme,
  DEFAULT_THEME,
  THEME_KEY,
  TOKENS,
} from '../src/ui/theme-model';

function luminance(colour: string): number {
  const channels = colour
    .replace('#', '')
    .match(/../g)!
    .map((channel) => {
      const value = Number.parseInt(channel, 16) / 255;
      return value <= 0.04045
        ? value / 12.92
        : ((value + 0.055) / 1.055) ** 2.4;
    });
  return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
}

function contrast(foreground: string, background: string): number {
  const values = [luminance(foreground), luminance(background)].sort(
    (a, b) => b - a,
  );
  return (values[0] + 0.05) / (values[1] + 0.05);
}

describe.each(['light', 'dark'] as const)(
  '%s semantic contrast contract',
  (mode) => {
    const tokens = TOKENS[mode];
    const surfaces = [
      'canvas',
      'surface',
      'surface-raised',
      'surface-hover',
      'surface-pressed',
      'surface-disabled',
    ] as const;
    for (const surface of surfaces) {
      for (const text of [
        'text-primary',
        'text-secondary',
        'text-muted',
        'text-disabled',
      ] as const) {
        it(`${text} remains readable on ${surface}`, () => {
          expect(
            contrast(tokens[text], tokens[surface]),
          ).toBeGreaterThanOrEqual(4.5);
        });
      }
      it(`control boundary is visible on ${surface}`, () => {
        expect(
          contrast(tokens['border-control'], tokens[surface]),
        ).toBeGreaterThanOrEqual(3);
      });
      for (const accent of Object.keys(
        TOKENS.accents,
      ) as (keyof typeof TOKENS.accents)[]) {
        it(`${accent} link/focus colour is readable on ${surface}`, () => {
          expect(
            contrast(TOKENS.accents[accent][mode], tokens[surface]),
          ).toBeGreaterThanOrEqual(4.5);
        });
      }
    }
    for (const status of ['info', 'success', 'warning', 'danger'] as const) {
      it(`${status} has independent readable text and border`, () => {
        expect(
          contrast(
            tokens[`status-${status}-text`],
            tokens[`status-${status}-background`],
          ),
        ).toBeGreaterThanOrEqual(4.5);
        expect(
          contrast(
            tokens[`status-${status}-border`],
            tokens[`status-${status}-background`],
          ),
        ).toBeGreaterThanOrEqual(3);
      });
    }
    for (const kind of ['add', 'remove', 'change'] as const) {
      it(`diff ${kind} text and marker remain readable`, () => {
        expect(
          contrast(
            tokens[`diff-${kind}-text`],
            tokens[`diff-${kind}-background`],
          ),
        ).toBeGreaterThanOrEqual(4.5);
        expect(
          contrast(
            tokens[`diff-${kind}-marker`],
            tokens[`diff-${kind}-background`],
          ),
        ).toBeGreaterThanOrEqual(3);
      });
    }
    for (const token of [
      'code-text',
      'code-comment',
      'syntax-keyword',
      'syntax-string',
      'syntax-number',
    ] as const) {
      it(`${token} is readable on code background`, () => {
        expect(
          contrast(tokens[token], tokens['code-background']),
        ).toBeGreaterThanOrEqual(4.5);
      });
    }
    for (const accent of Object.keys(
      TOKENS.accents,
    ) as (keyof typeof TOKENS.accents)[]) {
      it(`${accent} selection and solid action text remain readable`, () => {
        expect(
          contrast(tokens['text-inverse'], TOKENS.accents[accent][mode]),
        ).toBeGreaterThanOrEqual(4.5);
      });
    }
    for (const series of [1, 2, 3, 4, 5, 6] as const) {
      it(`chart series ${series} is visible against canvas`, () => {
        expect(
          contrast(tokens[`chart-series-${series}`], tokens.canvas),
        ).toBeGreaterThanOrEqual(3);
      });
    }
  },
);

describe('device preference input boundary', () => {
  beforeEach(() => {
    localStorage.clear();
    document.documentElement.removeAttribute('style');
    for (const key of Object.keys(document.documentElement.dataset))
      delete document.documentElement.dataset[key];
  });
  afterEach(() => vi.restoreAllMocks());

  it.each([
    'null',
    '[]',
    '"dark"',
    '{',
    '{"version":999,"appearance":"dark"}',
    '{"version":1,"appearance":"purple","accent":"neon"}',
  ])('invalid stored preference %s has safe defaults', (stored) => {
    localStorage.setItem(THEME_KEY, stored);
    expect(bootstrapTheme(TOKENS)).toEqual(DEFAULT_THEME);
    expect(document.documentElement.dataset.theme).toBe('light');
  });

  it('migrates only the supported preference fields, excluding arbitrary properties', () => {
    localStorage.setItem(
      THEME_KEY,
      JSON.stringify({
        version: 0,
        appearance: 'dark',
        accent: 'teal',
        density: 'compact',
        reduce_transparency: true,
        csrf_token: 'synthetic-not-a-session',
        path: '/untrusted',
      }),
    );
    expect(bootstrapTheme(TOKENS)).toEqual({
      version: 1,
      appearance: 'dark',
      accent: 'teal',
      density: 'compact',
      reduce_transparency: true,
    });
    expect(document.documentElement.dataset).toMatchObject({
      theme: 'dark',
      accent: 'teal',
      density: 'compact',
      opaque: 'true',
    });
    expect(
      document.documentElement.style.getPropertyValue('--accent-solid'),
    ).toBe(TOKENS.accents.teal.dark);
  });

  it('continues with safe defaults if browser storage is unavailable', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new DOMException('Blocked', 'SecurityError');
    });
    expect(bootstrapTheme(TOKENS)).toEqual(DEFAULT_THEME);
  });

  it('resolves system appearance and transparency synchronously before a renderer is mounted', () => {
    vi.spyOn(window, 'matchMedia').mockImplementation(
      (query) =>
        ({
          matches: query.includes('dark') || query.includes('transparency'),
        }) as MediaQueryList,
    );
    expect(bootstrapTheme(TOKENS).appearance).toBe('system');
    expect(document.documentElement.dataset).toMatchObject({
      theme: 'dark',
      opaque: 'true',
    });
    expect(document.documentElement.style.colorScheme).toBe('dark');
  });
});
