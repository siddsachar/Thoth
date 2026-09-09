type ClientLifecycle = {
  setOnline(online: boolean): void;
  dispose(): void;
};
type PageLifecycle = {
  addEventListener(type: string, listener: EventListener): void;
  removeEventListener(type: string, listener: EventListener): void;
  navigator: { readonly onLine: boolean };
};

/** Suspend cached documents; returning pages reauthenticate without replaying work. */
export function bindPageLifecycle(
  client: ClientLifecycle,
  page: PageLifecycle = window,
): () => void {
  let cached = false;
  const online = () => client.setOnline(!cached && page.navigator.onLine);
  const hide: EventListener = (event) => {
    if ((event as PageTransitionEvent).persisted) {
      cached = true;
      online();
    } else client.dispose();
  };
  const show: EventListener = (event) => {
    if ((event as PageTransitionEvent).persisted) {
      cached = false;
      online();
    }
  };
  const listeners: [string, EventListener][] = [
    ['online', online],
    ['offline', online],
    ['pagehide', hide],
    ['pageshow', show],
  ];
  for (const [name, listener] of listeners)
    page.addEventListener(name, listener);
  online();
  return () => {
    for (const [name, listener] of listeners)
      page.removeEventListener(name, listener);
  };
}
