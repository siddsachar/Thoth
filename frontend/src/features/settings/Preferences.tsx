import { useState } from 'react';
import { Link } from 'react-router-dom';
import { Button, Field, Input, Select } from '../../ui/primitives';
import { useTheme } from '../../ui/theme';
import { useOverlay } from '../../ui/overlays';
import type { Accent, Appearance } from '../../ui/theme-model';
import { searchSettings } from './model';

export default function Preferences({ onReset }: { onReset?: () => void }) {
  const { preference, update } = useTheme();
  const { open, close, notify } = useOverlay();
  const [query, setQuery] = useState('');
  return (
    <div className="stack">
      <div className="field-row">
        <Field label="Appearance">
          <Select
            value={preference.appearance}
            onChange={(event) =>
              update({ appearance: event.target.value as Appearance })
            }
          >
            <option value="system">System</option>
            <option value="light">Light</option>
            <option value="dark">Dark</option>
          </Select>
        </Field>
        <Field label="Colour theme">
          <Select
            value={preference.accent}
            onChange={(event) =>
              update({ accent: event.target.value as Accent })
            }
          >
            <option value="blue">Blue</option>
            <option value="teal">Teal</option>
            <option value="violet">Violet</option>
            <option value="amber">Amber</option>
          </Select>
        </Field>
        <Field
          label="Density"
          hint="Touch controls always keep their full size."
        >
          <Select
            value={preference.density}
            onChange={(event) =>
              update({
                density: event.target.value as 'comfortable' | 'compact',
              })
            }
          >
            <option value="comfortable">Comfortable</option>
            <option value="compact">Compact</option>
          </Select>
        </Field>
      </div>
      <label className="check-field">
        <input
          type="checkbox"
          checked={preference.reduce_transparency}
          onChange={(event) =>
            update({ reduce_transparency: event.target.checked })
          }
        />
        Reduce transparency
      </label>
      {onReset && (
        <div>
          <Button
            onClick={() =>
              open({
                kind: 'alert',
                title: 'Reset layout?',
                description:
                  'Restore the default panel sizes and close sample panels. Your conversations and appearance stay saved.',
                confirmLabel: 'Reset layout',
                onConfirm: () => {
                  onReset();
                  notify('Layout reset');
                },
              })
            }
          >
            Reset layout
          </Button>
        </div>
      )}
      <hr />
      <h2>Find a setting</h2>
      <Field label="Search settings">
        <Input
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Search names and categories"
        />
      </Field>
      <nav aria-label="Settings">
        <ul className="settings-results">
          {searchSettings(query).map((leaf) => (
            <li key={leaf.id}>
              <Link to={leaf.href} onClick={close}>
                {leaf.label}
                <small>{leaf.category}</small>
              </Link>
            </li>
          ))}
        </ul>
      </nav>
      <p className="muted">
        Appearance is available here. Other settings open a safe link to the
        current application while this client is being built.
      </p>
    </div>
  );
}
