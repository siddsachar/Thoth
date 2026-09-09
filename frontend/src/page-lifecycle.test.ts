import { describe, expect, it, vi } from 'vitest';
import { bindPageLifecycle } from './page-lifecycle';

function setup() {
  const page = Object.assign(new EventTarget(), {
    navigator: { onLine: true },
  });
  const client = { setOnline: vi.fn(), dispose: vi.fn() };
  const release = bindPageLifecycle(client, page);
  const transition = (name: string, persisted: boolean) =>
    page.dispatchEvent(Object.assign(new Event(name), { persisted }));
  return { page, client, release, transition };
}
describe('page observation lifecycle', () => {
  it('reports network availability and releases all listeners', () => {
    const { page, client, release } = setup();
    expect(client.setOnline).toHaveBeenLastCalledWith(true);
    page.navigator.onLine = false;
    page.dispatchEvent(new Event('offline'));
    expect(client.setOnline).toHaveBeenLastCalledWith(false);
    page.navigator.onLine = true;
    page.dispatchEvent(new Event('online'));
    expect(client.setOnline).toHaveBeenLastCalledWith(true);
    release();
    client.setOnline.mockClear();
    page.dispatchEvent(new Event('offline'));
    page.dispatchEvent(new Event('pagehide'));
    expect(client.setOnline).not.toHaveBeenCalled();
    expect(client.dispose).not.toHaveBeenCalled();
  });
  it('suspends repeated cached visits without disposing or waking a hidden page', () => {
    const { page, client, transition } = setup();
    for (let index = 0; index < 3; index++) {
      transition('pagehide', true);
      expect(client.setOnline).toHaveBeenLastCalledWith(false);
      page.dispatchEvent(new Event('online'));
      expect(client.setOnline).toHaveBeenLastCalledWith(false);
      transition('pageshow', true);
      expect(client.setOnline).toHaveBeenLastCalledWith(true);
    }
    expect(client.dispose).not.toHaveBeenCalled();
    transition('pagehide', false);
    expect(client.dispose).toHaveBeenCalledOnce();
  });
  it('keeps an offline cached page suspended when restored', () => {
    const { page, client, transition } = setup();
    transition('pagehide', true);
    page.navigator.onLine = false;
    transition('pageshow', true);
    expect(client.setOnline).toHaveBeenLastCalledWith(false);
  });
});
