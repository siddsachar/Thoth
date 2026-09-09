import { describe, expect, it, vi } from 'vitest';
import { PanelSubscriptions } from './subscriptions';

describe('visible reference-counted panel views', () => {
  it('releases a synchronous source even when its first notification closes the view', () => {
    const manager = new PanelSubscriptions();
    const stop = vi.fn();
    const handle = manager.acquire(
      'sample',
      (notify) => {
        notify('1');
        return stop;
      },
      () => handle.release(),
      false,
    );
    handle.setVisible(true);
    expect(stop).toHaveBeenCalledTimes(1);
    expect(manager.metrics.active).toBe(0);
    expect(manager.metrics.references).toBe(0);
  });
  it('shares one source, suppresses hidden/unchanged renders and reconciles on return', () => {
    const manager = new PanelSubscriptions();
    let publish = (_revision: string) => {};
    const cleanup = vi.fn();
    const source = vi.fn((notify: (revision: string) => void) => {
      publish = notify;
      notify('1');
      return cleanup;
    });
    const first = vi.fn(),
      second = vi.fn();
    const a = manager.acquire('resource', source, first),
      b = manager.acquire('resource', source, second);
    expect(source).toHaveBeenCalledTimes(1);
    publish('1');
    expect(first).toHaveBeenCalledTimes(1);
    a.setVisible(false);
    publish('2');
    expect(first).toHaveBeenCalledTimes(1);
    expect(second).toHaveBeenLastCalledWith('2');
    a.setVisible(true);
    expect(first).toHaveBeenLastCalledWith('2');
    a.release();
    expect(cleanup).not.toHaveBeenCalled();
    b.release();
    expect(cleanup).toHaveBeenCalledTimes(1);
    expect(manager.metrics.active).toBe(0);
    expect(manager.metrics.references).toBe(0);
  });
  it('measures 60s hidden idle and 100 close/reopen cycles with no resource growth', async () => {
    vi.useFakeTimers();
    try {
      const manager = new PanelSubscriptions();
      const renders = vi.fn();
      const stop = vi.fn();
      const source = vi.fn((notify: (revision: string) => void) => {
        notify('1');
        return stop;
      });
      const hidden = manager.acquire('sample', source, renders, false);
      await vi.advanceTimersByTimeAsync(60000);
      expect(source).not.toHaveBeenCalled();
      expect(renders).not.toHaveBeenCalled();
      hidden.release();
      for (let i = 0; i < 100; i++) {
        const view = manager.acquire('sample', source, renders);
        view.release();
        view.release();
      }
      expect(manager.metrics).toEqual({
        subscriptions: 100,
        cleanups: 100,
        notifications: 100,
        active: 0,
        references: 0,
      });
      expect(vi.getTimerCount()).toBe(0);
      manager.dispose();
    } finally {
      vi.useRealTimers();
    }
  });
});
