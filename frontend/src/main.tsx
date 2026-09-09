import { Component, lazy, Suspense, type ReactNode } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter, Route, Routes } from 'react-router-dom';
import { createClientController } from './api';
import { selectClientPlatform } from './platform';
import { RuntimeContext } from './runtime';
import { bindPageLifecycle } from './page-lifecycle';
import { ThemeProvider } from './ui/theme';
import { OverlayProvider } from './ui/overlays';
import { EmptyState, ErrorState, Skeleton } from './ui/primitives';
import Workspace, { panelMetrics } from './features/shell/Workspace';
import './ui/styles.css';

const Gallery = lazy(() => import('./features/shell/Gallery'));
const SettingRoute = lazy(() => import('./features/settings/SettingRoute'));
const SettingsIndex = lazy(() => import('./features/settings/SettingsIndex'));
class RenderBoundary extends Component<
  { children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  render() {
    return this.state.failed ? (
      <main className="startup-error">
        <ErrorState
          title="The workspace could not open"
          action={
            <a className="button" href="/app-v2/">
              Reload workspace
            </a>
          }
        >
          Try reloading, or return to the <a href="/">current application</a>.
        </ErrorState>
      </main>
    ) : (
      this.props.children
    );
  }
}
async function start() {
  const query = new URLSearchParams(location.search).get('fixture');
  const fixture = [
    'normal',
    'incompatible',
    'unauthorized',
    'disconnected',
  ].includes(query ?? '')
    ? (query as 'normal' | 'incompatible' | 'unauthorized' | 'disconnected')
    : undefined;
  let fixtureTransport: unknown;
  const controller = await createClientController({
    fixture,
    onFixture: (transport) => {
      fixtureTransport = transport;
    },
  });
  let platform = selectClientPlatform(controller, undefined);
  if (import.meta.env.VITE_ENABLE_FIXTURES === '1' && fixtureTransport) {
    if (
      new URLSearchParams(location.search).get('fixturePlatform') === 'fake'
    ) {
      const { createFakePlatform } = await import('./platform/fake');
      platform = createFakePlatform();
    }
    Object.defineProperty(window, '__ROW_BOT_FIXTURE__', {
      value: {
        controller,
        transport: fixtureTransport,
        platform,
        panelMetrics,
      },
      configurable: true,
    });
  }
  createRoot(document.getElementById('root')!).render(
    <ThemeProvider>
      <RenderBoundary>
        <RuntimeContext.Provider value={{ controller, platform }}>
          <BrowserRouter basename="/app-v2">
            <OverlayProvider>
              <Suspense fallback={<Skeleton label="Opening workspace" />}>
                <Routes>
                  <Route path="/" element={<Workspace />}>
                    <Route path="primitives" element={<Gallery />} />
                    <Route path="settings" element={<SettingsIndex />} />
                    <Route
                      path="settings/:setting"
                      element={<SettingRoute />}
                    />
                    <Route
                      path="*"
                      element={
                        <EmptyState
                          title="View not found"
                          action={
                            <a className="button" href="/app-v2/">
                              Return to conversation
                            </a>
                          }
                        >
                          This view is not available.
                        </EmptyState>
                      }
                    />
                  </Route>
                </Routes>
              </Suspense>
            </OverlayProvider>
          </BrowserRouter>
        </RuntimeContext.Provider>
      </RenderBoundary>
    </ThemeProvider>,
  );
  bindPageLifecycle(controller);
  void controller.start();
}
void start().catch(() => {
  const root = document.getElementById('root')!;
  root.replaceChildren();
  const message = document.createElement('p');
  message.textContent =
    'The workspace could not start. Reload the page to try again.';
  const link = document.createElement('a');
  link.href = '/';
  link.textContent = 'Open current application';
  root.append(message, link);
});
