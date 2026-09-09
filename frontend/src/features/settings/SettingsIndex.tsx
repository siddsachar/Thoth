import { Link } from 'react-router-dom';
import { settingsGroups, resolveSetting } from './model';
export default function SettingsIndex() {
  return (
    <section className="route-surface stack" aria-label="Settings index">
      <h1>Settings</h1>
      {settingsGroups.map((group) => (
        <section className="stack" key={group.id}>
          <h2>{group.label}</h2>
          <ul className="settings-results">
            {group.leaves.map((label) => (
              <li key={label}>
                <Link to={resolveSetting(label)!.href}>{label}</Link>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </section>
  );
}
