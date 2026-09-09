import { createContext, useContext, useSyncExternalStore } from 'react';
import type { ClientController } from './api';
import type { ClientPlatform } from './platform';
export const RuntimeContext = createContext<{
  controller: ClientController;
  platform: ClientPlatform;
} | null>(null);
export function useRuntime() {
  const runtime = useContext(RuntimeContext);
  if (!runtime) throw new Error('RuntimeContext is required');
  return runtime;
}
export function useClientState() {
  const { controller } = useRuntime();
  return useSyncExternalStore(controller.subscribe, controller.getSnapshot);
}
