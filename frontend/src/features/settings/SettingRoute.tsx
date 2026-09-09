import { Navigate, useParams } from 'react-router-dom';
import { EmptyState } from '../../ui/primitives';
import { resolveSetting } from './model';
import Preferences from './Preferences';

export default function SettingRoute() {
  const { setting = 'preferences' } = useParams();
  const leaf = resolveSetting(setting);
  if (!leaf) return <Navigate to="/settings" replace />;
  return (
    <section
      className="route-surface"
      aria-label={leaf?.label ?? 'Unknown setting'}
    >
      {leaf?.id === 'preferences' ? (
        <>
          <h1>Preferences</h1>
          <Preferences />
        </>
      ) : (
        <EmptyState
          title={leaf?.label ?? 'Setting not found'}
          action={
            <a className="button" href="/">
              Open current application
            </a>
          }
        >
          This setting is available in the current application.
        </EmptyState>
      )}
    </section>
  );
}
