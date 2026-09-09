import { describe, expect, it } from 'vitest';
import {
  closePanel,
  createPanelLayout,
  focusPanel,
  movePanel,
  openPanel,
  panelPresentation,
  panelStatus,
  persistLayout,
  reconcileLayout,
  regionBounds,
  resetLayout,
  resizeRegion,
  restoreLayout,
  samplePanels,
  suggestPanel,
  toggleRegion,
} from './model';

describe('typed presentation-only panel registry', () => {
  it('deduplicates kind/resource/subresource and supports explicit duplicate views', () => {
    let state = openPanel(createPanelLayout(), samplePanels[0]);
    const first = state.activePanelId;
    state = openPanel(state, samplePanels[0]);
    expect(state.panels).toHaveLength(1);
    expect(state.activePanelId).toBe(first);
    state = openPanel(state, samplePanels[0], 'bottom', true);
    expect(state.panels).toHaveLength(2);
    state = movePanel(state, first!, 'bottom');
    expect(state.panels[0].placement).toBe('bottom');
    state = closePanel(state, state.activePanelId!);
    expect(state.activePanelId).toBe(first);
    state = closePanel(state, first!);
    expect(state.panels).toHaveLength(0);
    expect(state.activePanelId).toBeNull();
  });
  it('suggestions never open a panel or steal user focus', () => {
    const state = openPanel(createPanelLayout(), samplePanels[0]);
    const next = suggestPanel(state, samplePanels[1]);
    expect(next.activePanelId).toBe(state.activePanelId);
    expect(next.panels).toBe(state.panels);
    expect(next.suggestions).toHaveLength(1);
    expect(suggestPanel(next, samplePanels[1]).suggestions).toHaveLength(1);
  });
  it('distinguishes unknown, missing, stale, unauthorized and unavailable capability', () => {
    const descriptor = {
      panel_kind: 'fake.resource',
      title: 'Synthetic document',
      resource_ref: 'document-1',
      resource_revision: '2',
    };
    const context = {
      capabilities: new Set(['fixture.read']),
      resources: new Map(),
    };
    expect(
      panelStatus(
        { ...descriptor, panel_kind: 'untrusted.component' },
        context,
      ),
    ).toBe('unsupported');
    expect(panelStatus(descriptor, context)).toBe('missing');
    context.resources.set('document-1', {
      kind: 'document',
      revision: '1',
      status: 'available',
    });
    expect(panelStatus(descriptor, context)).toBe('stale');
    context.resources.set('document-1', {
      kind: 'document',
      revision: '2',
      status: 'unauthorized',
    });
    expect(panelStatus(descriptor, context)).toBe('unauthorized');
    context.resources.set('document-1', {
      kind: 'document',
      revision: '2',
      status: 'available',
    });
    expect(panelStatus(descriptor, context)).toBe('ready');
    context.capabilities.clear();
    expect(panelStatus(descriptor, context)).toBe('capability-unavailable');
  });
  it.each([
    [1440, 900],
    [1280, 720],
    [820, 1180],
    [390, 844],
    [360, 800],
  ])(
    'clamps desktop bounds and transforms compact %dx%d without changing instance',
    (width, height) => {
      let state = openPanel(createPanelLayout(), samplePanels[0]);
      const instance = state.panels[0];
      state = reconcileLayout(state, width, height);
      state = resizeRegion(state, 'side', 10000);
      expect(state.side.size).toBeLessThanOrEqual(
        regionBounds(state, 'side').max,
      );
      expect(state.panels[0]).toBe(instance);
      expect(panelPresentation(state, instance)).toBe(
        width >= 1024 ? 'side' : 'sheet',
      );
      const before = state.side.size;
      state = toggleRegion(state, 'side');
      expect(state.side.collapsed).toBe(true);
      state = toggleRegion(state, 'side');
      expect(state.side.size).toBe(before);
      expect(state.side.collapsed).toBe(false);
    },
  );
  it('persists one width class, migrates v0, clamps stale sizes and resets safely', () => {
    let state = openPanel(createPanelLayout(), samplePanels[0]);
    state = resizeRegion(state, 'side', 480);
    state = toggleRegion(state, 'side');
    const restored = restoreLayout(persistLayout(state), 1440, 900);
    expect(restored.panels).toEqual(state.panels);
    expect(restored.side).toEqual(state.side);
    expect(restoreLayout(persistLayout(state), 390, 844).panels).toHaveLength(
      0,
    );
    expect(
      restoreLayout('{"version":0,"side":99999}', 1280, 720).side.size,
    ).toBeLessThan(720);
    for (const raw of [
      '{',
      '{"version":999}',
      '{"version":1,"panels":[{"descriptor":{"panel_kind":"<script>"}}]}',
    ])
      expect(restoreLayout(raw, 390, 844).panels).toHaveLength(0);
    expect(resetLayout(state).panels).toHaveLength(0);
    expect(focusPanel(state, 'missing')).toBe(state);
    const invalid = {
      version: 1,
      panels: [{ ...state.panels[0], instance_id: `panel-${'9'.repeat(400)}` }],
    };
    const safe = restoreLayout(JSON.stringify(invalid), 1440, 900);
    expect(safe.panels).toHaveLength(0);
    expect(
      Number.isSafeInteger(openPanel(safe, samplePanels[0]).nextInstance),
    ).toBe(true);
  });
  it('preserves Back to conversation across compact refresh while retaining panels', () => {
    const opened = openPanel(createPanelLayout(390, 844), samplePanels[1]);
    const conversation = focusPanel(opened, null);
    const restored = restoreLayout(persistLayout(conversation), 390, 844);
    expect(restored.activePanelId).toBeNull();
    expect(restored.panels).toEqual(opened.panels);
    const selected = restoreLayout(persistLayout(opened), 390, 844);
    expect(selected.activePanelId).toBe(opened.activePanelId);
    const invalid = JSON.parse(persistLayout(opened));
    invalid.activePanelId = 'missing-panel';
    expect(restoreLayout(JSON.stringify(invalid), 390, 844).activePanelId).toBe(
      opened.activePanelId,
    );
  });
});
