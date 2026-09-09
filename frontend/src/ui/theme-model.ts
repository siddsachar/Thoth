export type Appearance = 'system' | 'light' | 'dark';
export type Accent = 'blue' | 'teal' | 'violet' | 'amber';
export type ThemePreference = {
  version: 1;
  appearance: Appearance;
  accent: Accent;
  density: 'comfortable' | 'compact';
  reduce_transparency: boolean;
};
export const THEME_KEY = 'row-bot.appearance.v1';
export const DEFAULT_THEME: ThemePreference = {
  version: 1,
  appearance: 'system',
  accent: 'blue',
  density: 'comfortable',
  reduce_transparency: false,
};
export const TOKENS = {
  light: {
    canvas: '#F7F9FB',
    surface: '#FFFFFF',
    'surface-raised': '#FFFFFF',
    'surface-hover': '#EAF0F6',
    'surface-pressed': '#DEE7F0',
    'surface-disabled': '#EDF0F3',
    'text-primary': '#17212B',
    'text-secondary': '#465564',
    'text-muted': '#576879',
    'text-disabled': '#576879',
    'text-inverse': '#FFFFFF',
    'border-subtle': '#D6DEE6',
    'border-control': '#647586',
    'code-background': '#EEF2F6',
    'code-text': '#17212B',
    'code-comment': '#526475',
    'syntax-keyword': '#65459B',
    'syntax-string': '#175A3C',
    'syntax-number': '#80550D',
    'status-info-text': '#254E77',
    'status-info-background': '#E8F0F9',
    'status-info-border': '#254E77',
    'status-success-text': '#175A3C',
    'status-success-background': '#E7F4ED',
    'status-success-border': '#175A3C',
    'status-warning-text': '#704709',
    'status-warning-background': '#FFF1CF',
    'status-warning-border': '#704709',
    'status-danger-text': '#8B2735',
    'status-danger-background': '#FBEAEC',
    'status-danger-border': '#8B2735',
    'diff-add-text': '#175A3C',
    'diff-add-background': '#E7F4ED',
    'diff-add-marker': '#175A3C',
    'diff-remove-text': '#8B2735',
    'diff-remove-background': '#FBEAEC',
    'diff-remove-marker': '#8B2735',
    'diff-change-text': '#704709',
    'diff-change-background': '#FFF1CF',
    'diff-change-marker': '#704709',
    'chart-series-1': '#345E87',
    'chart-series-2': '#086A68',
    'chart-series-3': '#65459B',
    'chart-series-4': '#80550D',
    'chart-series-5': '#8B2735',
    'chart-series-6': '#465564',
    'chart-grid': '#647586',
    'chart-axis': '#465564',
    'artifact-canvas-chrome': '#EEF2F6',
    'artifact-page-border': '#647586',
    'overlay-scrim': '#17212B80',
  },
  dark: {
    canvas: '#101820',
    surface: '#18232E',
    'surface-raised': '#22313F',
    'surface-hover': '#293B4D',
    'surface-pressed': '#31465B',
    'surface-disabled': '#22313F',
    'text-primary': '#F1F5F9',
    'text-secondary': '#C6D1DC',
    'text-muted': '#A6B6C7',
    'text-disabled': '#A6B6C7',
    'text-inverse': '#101820',
    'border-subtle': '#3B4B5C',
    'border-control': '#8294A7',
    'code-background': '#111B25',
    'code-text': '#F1F5F9',
    'code-comment': '#A6B6C7',
    'syntax-keyword': '#BEA7EA',
    'syntax-string': '#A7E3BC',
    'syntax-number': '#E8BE68',
    'status-info-text': '#BAD6F3',
    'status-info-background': '#20374D',
    'status-info-border': '#BAD6F3',
    'status-success-text': '#A7E3BC',
    'status-success-background': '#183E2C',
    'status-success-border': '#A7E3BC',
    'status-warning-text': '#F4D48D',
    'status-warning-background': '#443414',
    'status-warning-border': '#F4D48D',
    'status-danger-text': '#F4AFB8',
    'status-danger-background': '#491F29',
    'status-danger-border': '#F4AFB8',
    'diff-add-text': '#A7E3BC',
    'diff-add-background': '#183E2C',
    'diff-add-marker': '#A7E3BC',
    'diff-remove-text': '#F4AFB8',
    'diff-remove-background': '#491F29',
    'diff-remove-marker': '#F4AFB8',
    'diff-change-text': '#F4D48D',
    'diff-change-background': '#443414',
    'diff-change-marker': '#F4D48D',
    'chart-series-1': '#92B5D8',
    'chart-series-2': '#72C9C2',
    'chart-series-3': '#BEA7EA',
    'chart-series-4': '#E8BE68',
    'chart-series-5': '#F4AFB8',
    'chart-series-6': '#C6D1DC',
    'chart-grid': '#8294A7',
    'chart-axis': '#C6D1DC',
    'artifact-canvas-chrome': '#111B25',
    'artifact-page-border': '#8294A7',
    'overlay-scrim': '#101820CC',
  },
  accents: {
    blue: { light: '#345E87', dark: '#92B5D8' },
    teal: { light: '#086A68', dark: '#72C9C2' },
    violet: { light: '#65459B', dark: '#BEA7EA' },
    amber: { light: '#80550D', dark: '#E8BE68' },
  },
};

/** Self-contained: Vite embeds this same function before the first stylesheet. */
export function bootstrapTheme(
  tokens: typeof TOKENS,
  supplied?: ThemePreference,
): ThemePreference {
  const preference: ThemePreference = {
    version: 1,
    appearance: 'system',
    accent: 'blue',
    density: 'comfortable',
    reduce_transparency: false,
  };
  try {
    const saved =
      supplied ??
      JSON.parse(localStorage.getItem('row-bot.appearance.v1') ?? 'null');
    if (saved && (saved.version === 1 || saved.version === 0)) {
      if (['system', 'light', 'dark'].includes(saved.appearance))
        preference.appearance = saved.appearance;
      if (['blue', 'teal', 'violet', 'amber'].includes(saved.accent))
        preference.accent = saved.accent;
      if (saved.density === 'compact') preference.density = 'compact';
      preference.reduce_transparency = saved.reduce_transparency === true;
    }
  } catch {
    /* Private browsing or invalid data uses safe per-device defaults. */
  }
  const mode =
    preference.appearance === 'system'
      ? matchMedia('(prefers-color-scheme: dark)').matches
        ? 'dark'
        : 'light'
      : preference.appearance;
  const root = document.documentElement;
  Object.assign(root.dataset, {
    appearance: preference.appearance,
    theme: mode,
    accent: preference.accent,
    density: preference.density,
    opaque: String(
      preference.reduce_transparency ||
        matchMedia('(prefers-reduced-transparency: reduce)').matches,
    ),
  });
  root.style.colorScheme = mode;
  for (const [name, value] of Object.entries(tokens[mode]))
    root.style.setProperty(`--${name}`, value);
  const accent = tokens.accents[preference.accent][mode];
  for (const name of [
    'accent-solid',
    'focus-ring',
    'text-link',
    'artifact-selection',
    'artifact-handle',
    'selection-background',
  ])
    root.style.setProperty(`--${name}`, accent);
  root.style.setProperty('--accent-on-solid', tokens[mode]['text-inverse']);
  root.style.setProperty('--selection-text', tokens[mode]['text-inverse']);
  root.style.setProperty(
    '--accent-subtle',
    tokens[mode]['status-info-background'],
  );
  return preference;
}
