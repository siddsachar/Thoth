import { isPanelDescriptor } from '../../api/types';
import type { PanelDescriptor, ResourceBinding } from '../../api/types';
export type { PanelDescriptor } from '../../api/types';

export type WidthClass = 'desktop' | 'tablet' | 'phone';
export type PanelPlacement = 'side' | 'bottom';
export type PanelStatus =
  | 'ready'
  | 'missing'
  | 'unauthorized'
  | 'stale'
  | 'unsupported'
  | 'capability-unavailable';
export type PanelInstance = {
  instance_id: string;
  descriptor: PanelDescriptor;
  placement: PanelPlacement;
  visibility: 'visible' | 'collapsed';
};
export type Region = { size: number; restoreSize: number; collapsed: boolean };
export type PanelLayout = {
  version: 1;
  widthClass: WidthClass;
  width: number;
  height: number;
  navigation: Region;
  side: Region;
  bottom: Region;
  panels: PanelInstance[];
  activePanelId: string | null;
  suggestions: PanelDescriptor[];
  nextInstance: number;
};
export type PanelRegistration = {
  title: string;
  resourceKinds: readonly ResourceBinding['kind'][];
  requiresResource: boolean;
  capabilities: readonly string[];
  compact: 'tab' | 'sheet';
};
/** Bundled fake renderers only. Server descriptors cannot register code or routes. */
export const panelRegistry = {
  'fake.info': {
    title: 'Sample information',
    resourceKinds: [],
    requiresResource: false,
    capabilities: [],
    compact: 'sheet',
  },
  'fake.activity': {
    title: 'Sample activity',
    resourceKinds: [],
    requiresResource: false,
    capabilities: [],
    compact: 'tab',
  },
  'fake.resource': {
    title: 'Sample resource',
    resourceKinds: ['document'],
    requiresResource: true,
    capabilities: ['fixture.read'],
    compact: 'tab',
  },
} as const satisfies Record<string, PanelRegistration>;
export const samplePanels: readonly PanelDescriptor[] = [
  { panel_kind: 'fake.info', title: 'Workspace notes' },
  { panel_kind: 'fake.activity', title: 'Activity preview' },
];
export function widthClass(width: number): WidthClass {
  return width >= 1024 ? 'desktop' : width >= 768 ? 'tablet' : 'phone';
}
const clamp = (value: number, min: number, max: number) =>
  Math.max(
    min,
    Math.min(Number.isFinite(value) ? value : min, Math.max(min, max)),
  );
export function regionBounds(
  layout: Pick<PanelLayout, 'width' | 'height' | 'navigation'>,
  region: 'navigation' | PanelPlacement,
): { min: number; max: number } {
  if (region === 'navigation') return { min: 200, max: 320 };
  if (region === 'bottom')
    return {
      min: 160,
      max: Math.max(160, Math.min(layout.height * 0.45, layout.height - 240)),
    };
  const navigation = layout.navigation.collapsed ? 48 : layout.navigation.size;
  return {
    min: 320,
    max: Math.max(
      320,
      Math.min(
        720,
        (layout.width - navigation - 24) * 0.55,
        layout.width - navigation - 424,
      ),
    ),
  };
}
export function createPanelLayout(width = 1440, height = 900): PanelLayout {
  return reconcileLayout(
    {
      version: 1,
      widthClass: widthClass(width),
      width,
      height,
      navigation: { size: 240, restoreSize: 240, collapsed: false },
      side: { size: 420, restoreSize: 420, collapsed: false },
      bottom: { size: 240, restoreSize: 240, collapsed: false },
      panels: [],
      activePanelId: null,
      suggestions: [],
      nextInstance: 1,
    },
    width,
    height,
  );
}
export function reconcileLayout(
  layout: PanelLayout,
  width: number,
  height: number,
): PanelLayout {
  const next = {
    ...layout,
    width: Math.max(1, width),
    height: Math.max(1, height),
    widthClass: widthClass(width),
    navigation: { ...layout.navigation },
  };
  // Preserve the 400px conversation and 320px resource minima before a desktop split.
  if (widthClass(width) === 'desktop' && width < next.navigation.size + 744)
    next.navigation.collapsed = true;
  for (const region of ['navigation', 'side', 'bottom'] as const) {
    const bounds = regionBounds(next, region);
    next[region] = {
      ...next[region],
      size: clamp(next[region].size, bounds.min, bounds.max),
      restoreSize: clamp(next[region].restoreSize, bounds.min, bounds.max),
    };
  }
  return next;
}
export function panelKey(descriptor: PanelDescriptor): string {
  return JSON.stringify([
    descriptor.panel_kind,
    descriptor.resource_ref ?? '',
    descriptor.subresource_key ?? '',
  ]);
}
export function panelPresentation(
  layout: PanelLayout,
  panel: PanelInstance,
): 'side' | 'bottom' | 'tab' | 'sheet' {
  if (
    layout.widthClass === 'desktop' &&
    layout.width >=
      (layout.navigation.collapsed ? 48 : layout.navigation.size) + 744
  )
    return panel.placement;
  return (
    panelRegistry[panel.descriptor.panel_kind as keyof typeof panelRegistry]
      ?.compact ?? 'tab'
  );
}
export function panelStatus(
  descriptor: PanelDescriptor,
  context: {
    capabilities: ReadonlySet<string>;
    resources: ReadonlyMap<
      string,
      {
        kind: ResourceBinding['kind'];
        revision: string;
        status: 'available' | 'missing' | 'unauthorized' | 'stale';
      }
    >;
  },
): PanelStatus {
  const registration: PanelRegistration | undefined =
    panelRegistry[descriptor.panel_kind as keyof typeof panelRegistry];
  if (!registration) return 'unsupported';
  if (
    [
      ...registration.capabilities,
      ...(descriptor.required_capabilities ?? []),
    ].some((capability) => !context.capabilities.has(capability))
  )
    return 'capability-unavailable';
  if (!registration.requiresResource && !descriptor.resource_ref)
    return 'ready';
  if (!descriptor.resource_ref) return 'missing';
  const resource = context.resources.get(descriptor.resource_ref);
  if (!resource) return 'missing';
  if (resource.status === 'unauthorized') return 'unauthorized';
  if (resource.status === 'missing') return 'missing';
  if (
    !registration.resourceKinds.includes(resource.kind) ||
    (descriptor.resource_kind && descriptor.resource_kind !== resource.kind)
  )
    return 'unsupported';
  if (
    resource.status === 'stale' ||
    (descriptor.resource_revision &&
      descriptor.resource_revision !== resource.revision)
  )
    return 'stale';
  return 'ready';
}
export function openPanel(
  layout: PanelLayout,
  descriptor: PanelDescriptor,
  placement: PanelPlacement = 'side',
  duplicate = false,
): PanelLayout {
  const known = layout.panels.find(
    (panel) => panelKey(panel.descriptor) === panelKey(descriptor),
  );
  if (known && !duplicate) return focusPanel(layout, known.instance_id);
  if (layout.panels.length >= 20) return layout;
  const instance_id = `panel-${layout.nextInstance}`;
  return {
    ...layout,
    nextInstance: layout.nextInstance + 1,
    activePanelId: instance_id,
    [placement]: { ...layout[placement], collapsed: false },
    panels: [
      ...layout.panels,
      {
        instance_id,
        descriptor: { ...descriptor },
        placement,
        visibility: 'visible',
      },
    ],
  };
}
export function focusPanel(
  layout: PanelLayout,
  id: string | null,
): PanelLayout {
  const panel = layout.panels.find((value) => value.instance_id === id);
  if (!panel) return id === null ? { ...layout, activePanelId: null } : layout;
  return {
    ...layout,
    activePanelId: id,
    [panel.placement]: { ...layout[panel.placement], collapsed: false },
    panels: layout.panels.map((value) =>
      value.instance_id === id ? { ...value, visibility: 'visible' } : value,
    ),
  };
}
export function closePanel(layout: PanelLayout, id: string): PanelLayout {
  const index = layout.panels.findIndex((value) => value.instance_id === id);
  if (index < 0) return layout;
  const panels = layout.panels.filter((value) => value.instance_id !== id);
  return {
    ...layout,
    panels,
    activePanelId:
      layout.activePanelId === id
        ? (panels[Math.min(index, panels.length - 1)]?.instance_id ?? null)
        : layout.activePanelId,
  };
}
export function movePanel(
  layout: PanelLayout,
  id: string,
  placement: PanelPlacement,
): PanelLayout {
  return {
    ...layout,
    [placement]: { ...layout[placement], collapsed: false },
    panels: layout.panels.map((value) =>
      value.instance_id === id ? { ...value, placement } : value,
    ),
  };
}
export function resizeRegion(
  layout: PanelLayout,
  region: 'navigation' | PanelPlacement,
  size: number,
): PanelLayout {
  const { min, max } = regionBounds(layout, region);
  const bounded = clamp(size, min, max);
  return reconcileLayout(
    {
      ...layout,
      [region]: { size: bounded, restoreSize: bounded, collapsed: false },
    },
    layout.width,
    layout.height,
  );
}
export function toggleRegion(
  layout: PanelLayout,
  region: 'navigation' | PanelPlacement,
): PanelLayout {
  const value = layout[region];
  return reconcileLayout(
    {
      ...layout,
      [region]: value.collapsed
        ? { ...value, size: value.restoreSize, collapsed: false }
        : { ...value, restoreSize: value.size, collapsed: true },
    },
    layout.width,
    layout.height,
  );
}
export function resetLayout(layout: PanelLayout): PanelLayout {
  return createPanelLayout(layout.width, layout.height);
}

/** Suggestions are advisory and cannot focus, open, move or select a conversation. */
export function suggestPanel(
  layout: PanelLayout,
  descriptor: PanelDescriptor,
): PanelLayout {
  if (
    layout.suggestions.some((value) => panelKey(value) === panelKey(descriptor))
  )
    return layout;
  return {
    ...layout,
    suggestions: [...layout.suggestions, descriptor].slice(-20),
  };
}
export function dismissSuggestion(
  layout: PanelLayout,
  descriptor: PanelDescriptor,
): PanelLayout {
  return {
    ...layout,
    suggestions: layout.suggestions.filter(
      (value) => panelKey(value) !== panelKey(descriptor),
    ),
  };
}

export function persistLayout(layout: PanelLayout): string {
  return JSON.stringify({
    version: 1,
    widthClass: layout.widthClass,
    navigation: layout.navigation,
    side: layout.side,
    bottom: layout.bottom,
    panels: layout.panels,
    activePanelId: layout.activePanelId,
    nextInstance: layout.nextInstance,
  });
}
export function restoreLayout(
  serialized: string | null,
  width: number,
  height: number,
): PanelLayout {
  const fallback = createPanelLayout(width, height);
  if (!serialized || serialized.length > 65536) return fallback;
  try {
    const value = JSON.parse(serialized) as Record<string, unknown>;
    if (
      value.widthClass !== undefined &&
      value.widthClass !== fallback.widthClass
    )
      return fallback;
    if (value.version !== 1 && value.version !== 0) return fallback;
    let layout = { ...fallback };
    // v0 stored naked region sizes; v1 stores collapse/restore as well.
    for (const key of ['navigation', 'side', 'bottom'] as const) {
      const item = value[key];
      if (value.version === 0 && typeof item === 'number')
        layout = resizeRegion(layout, key, item);
      if (item && typeof item === 'object') {
        const region = item as Partial<Region>;
        if (
          typeof region.size === 'number' &&
          typeof region.restoreSize === 'number' &&
          typeof region.collapsed === 'boolean'
        )
          layout[key] = region as Region;
      }
    }
    const ids = new Set<string>();
    if (Array.isArray(value.panels))
      layout.panels = value.panels
        .slice(0, 20)
        .filter((item): item is PanelInstance => {
          if (
            !item ||
            typeof item !== 'object' ||
            typeof item.instance_id !== 'string' ||
            !/^panel-\d{1,15}$/.test(item.instance_id) ||
            ids.has(item.instance_id) ||
            !isPanelDescriptor(item.descriptor) ||
            !['side', 'bottom'].includes(item.placement) ||
            !['visible', 'collapsed'].includes(item.visibility)
          )
            return false;
          ids.add(item.instance_id);
          return true;
        });
    layout.nextInstance =
      Math.max(
        0,
        ...layout.panels.map((panel) => Number(panel.instance_id.slice(6))),
      ) + 1;
    layout.activePanelId =
      value.activePanelId === null
        ? null
        : typeof value.activePanelId === 'string' &&
            ids.has(value.activePanelId)
          ? value.activePanelId
          : (layout.panels[0]?.instance_id ?? null);
    return reconcileLayout(layout, width, height);
  } catch {
    return fallback;
  }
}
export function layoutStorageKey(profile: string, width: number): string {
  return `row-bot:layout:v1:${profile}:${widthClass(width)}`;
}
