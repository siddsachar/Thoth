import { useEffect, useState } from 'react';
import {
  layoutStorageKey,
  persistLayout,
  reconcileLayout,
  restoreLayout,
  type PanelLayout,
} from '../panels/model';

function read(width: number, height: number): PanelLayout {
  try {
    return restoreLayout(
      localStorage.getItem(layoutStorageKey('local', width)),
      width,
      height,
    );
  } catch {
    return restoreLayout(null, width, height);
  }
}
export function useWorkspaceLayout() {
  const [layout, setLayout] = useState(() =>
    read(window.innerWidth, window.innerHeight - 64),
  );
  useEffect(() => {
    const resize = () =>
      setLayout((previous) => {
        const next = reconcileLayout(
          previous,
          window.innerWidth,
          window.innerHeight - 64,
        );
        if (next.widthClass === previous.widthClass) return next;
        const restored = read(window.innerWidth, window.innerHeight - 64);
        return {
          ...restored,
          panels: previous.panels,
          activePanelId: previous.activePanelId,
          nextInstance: previous.nextInstance,
          suggestions: previous.suggestions,
        };
      });
    window.addEventListener('resize', resize);
    return () => window.removeEventListener('resize', resize);
  }, []);
  useEffect(() => {
    try {
      localStorage.setItem(
        layoutStorageKey('local', layout.width),
        persistLayout(layout),
      );
    } catch {
      /* A blocked/full store still allows session-only layout changes. */
    }
  }, [layout]);
  return [layout, setLayout] as const;
}
