import { afterEach, describe, expect, it, vi } from 'vitest';
import { createBrowserPlatform } from './browser';
import { createFakePlatform } from './fake';
import { selectClientPlatform } from './index';
import { createPyWebViewPlatform } from './native';
import type { MediaTransport } from './types';
import { safeExternalUrl } from './types';

const attachment = {
  attachment_ref: 'fixture',
  name: 'fixture.txt',
  mime_type: 'application/octet-stream' as const,
  size_bytes: 7,
  revision: '1',
};
const media = (): MediaTransport => ({
  upload: vi.fn().mockResolvedValue(attachment),
  download: vi.fn().mockResolvedValue(new Blob(['fixture'])),
});
afterEach(() => {
  vi.restoreAllMocks();
  document.body.innerHTML = '';
});

describe('browser capabilities', () => {
  it('uses browser APIs and never infers native authority from viewport or spoofed globals', async () => {
    const legacy = vi.fn();
    Object.assign(window, {
      pywebview: { api: { choose_file: legacy } },
      __ROW_BOT_NATIVE__: true,
    });
    const adapter = selectClientPlatform(media(), undefined);
    expect(await adapter.discover()).toMatchObject({
      status: 'ok',
      value: { kind: 'browser' },
    });
    expect(await adapter.managedWindow('/app-v2/')).toMatchObject({
      status: 'unavailable',
    });
    expect(legacy).not.toHaveBeenCalled();
  });

  it('returns selected File objects without local paths and cleans cancellation listeners', async () => {
    vi.spyOn(HTMLInputElement.prototype, 'click').mockImplementation(
      () => undefined,
    );
    const adapter = createBrowserPlatform(media());
    const picked = adapter.selectFile();
    const input = document.querySelector('input')!;
    const file = new File(['fixture'], 'fixture.txt');
    Object.defineProperty(input, 'files', { value: [file] });
    input.dispatchEvent(new Event('change'));
    expect(await picked).toEqual({
      status: 'ok',
      value: { kind: 'file', files: [file] },
    });
    expect(document.querySelector('input')).toBeNull();
    const cancelled = adapter.selectFile();
    document.querySelector('input')!.dispatchEvent(new Event('cancel'));
    expect(await cancelled).toEqual({ status: 'cancelled' });
  });

  it('bounds pending selection and supports abort without a file or OS window', async () => {
    vi.spyOn(HTMLInputElement.prototype, 'click').mockImplementation(
      () => undefined,
    );
    const adapter = createBrowserPlatform(media());
    const abort = new AbortController();
    const selection = adapter.selectFile(abort.signal);
    expect(await adapter.selectFile()).toMatchObject({
      status: 'unavailable',
      reason: 'selection_in_progress',
    });
    abort.abort();
    expect(await selection).toEqual({ status: 'cancelled' });
    expect(document.querySelector('input')).toBeNull();
  });

  it('uses only the injected authenticated transport for upload and download', async () => {
    const transport = media();
    const adapter = createBrowserPlatform(transport);
    const file = new File(['fixture'], 'fixture.txt');
    expect(await adapter.upload('conversation', file)).toEqual({
      status: 'ok',
      value: attachment,
    });
    expect(transport.upload).toHaveBeenCalledWith(
      'conversation',
      file,
      undefined,
    );
    vi.stubGlobal(
      'URL',
      class extends URL {
        static createObjectURL = vi.fn().mockReturnValue('blob:fixture');
        static revokeObjectURL = vi.fn();
      },
    );
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(
      () => undefined,
    );
    expect(await adapter.save('fixture', 'fixture.txt')).toEqual({
      status: 'ok',
      value: null,
    });
    expect(transport.download).toHaveBeenCalledWith('fixture', undefined);
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:fixture');
    vi.unstubAllGlobals();
  });

  it('declares permission failure and unavailable clipboard without hidden fallbacks', async () => {
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: {
        readText: vi.fn().mockRejectedValue(new Error('private sentinel')),
      },
    });
    const adapter = createBrowserPlatform(media());
    expect(await adapter.readClipboard()).toEqual({
      status: 'unavailable',
      reason: 'operation_failed',
    });
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: undefined,
    });
    expect(await adapter.readClipboard()).toEqual({
      status: 'unavailable',
      reason: 'unsupported',
    });
  });

  it('rejects unsafe external URLs and download paths before any effect', async () => {
    const transport = media();
    const adapter = createBrowserPlatform(transport);
    for (const url of [
      'javascript:alert(1)',
      'file:///secret',
      'https://user:secret@example.invalid',
      'https://a\\b',
      '//example.invalid',
    ]) {
      expect(safeExternalUrl(url)).toBeUndefined();
      expect(await adapter.openExternal(url)).toMatchObject({
        status: 'unavailable',
      });
    }
    expect(await adapter.save('fixture', '../secret')).toMatchObject({
      status: 'unavailable',
    });
    expect(transport.download).not.toHaveBeenCalled();
  });

  it('uses permission-controlled clipboard and an isolated external-link request', async () => {
    const readText = vi.fn().mockResolvedValue('fixture');
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { readText, writeText },
    });
    const adapter = createBrowserPlatform(media());
    expect(await adapter.readClipboard()).toEqual({
      status: 'ok',
      value: 'fixture',
    });
    expect(await adapter.writeClipboard('fixture')).toEqual({
      status: 'ok',
      value: null,
    });
    expect(writeText).toHaveBeenCalledWith('fixture');
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(function (this: HTMLAnchorElement) {
        expect(this.rel).toBe('noopener noreferrer');
        expect(this.target).toBe('_blank');
      });
    expect(await adapter.openExternal('https://example.invalid/help')).toEqual({
      status: 'ok',
      value: null,
    });
    expect(click).toHaveBeenCalledTimes(1);
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: undefined,
    });
  });

  it('does not request picker, clipboard or link interaction without user activation', async () => {
    Object.defineProperty(navigator, 'userActivation', {
      configurable: true,
      value: { isActive: false },
    });
    const adapter = createBrowserPlatform(media());
    expect(await adapter.selectFile()).toEqual({
      status: 'unavailable',
      reason: 'user_gesture_required',
    });
    expect(await adapter.readClipboard()).toEqual({
      status: 'unavailable',
      reason: 'user_gesture_required',
    });
    expect(await adapter.openExternal('https://example.invalid')).toEqual({
      status: 'unavailable',
      reason: 'user_gesture_required',
    });
    expect(document.querySelector('input')).toBeNull();
    Object.defineProperty(navigator, 'userActivation', {
      configurable: true,
      value: undefined,
    });
  });

  it('selects browser directory files only when directory inputs are supported', async () => {
    vi.spyOn(HTMLInputElement.prototype, 'click').mockImplementation(
      () => undefined,
    );
    const descriptor = Object.getOwnPropertyDescriptor(
      HTMLInputElement.prototype,
      'webkitdirectory',
    );
    Object.defineProperty(HTMLInputElement.prototype, 'webkitdirectory', {
      configurable: true,
      writable: true,
      value: false,
    });
    const adapter = createBrowserPlatform(media());
    const pending = adapter.selectFolder();
    const input = document.querySelector('input')!;
    expect(input.webkitdirectory).toBe(true);
    const file = new File(['fixture'], 'fixture.txt');
    Object.defineProperty(input, 'files', { value: [file] });
    input.dispatchEvent(new Event('change'));
    expect(await pending).toEqual({
      status: 'ok',
      value: { kind: 'folder', files: [file] },
    });
    if (descriptor)
      Object.defineProperty(
        HTMLInputElement.prototype,
        'webkitdirectory',
        descriptor,
      );
    else Reflect.deleteProperty(HTMLInputElement.prototype, 'webkitdirectory');
  });
});

describe('safe native and fake capabilities', () => {
  it.each(['file', 'folder', 'save'] as const)(
    'discards the late native %s completion after abort',
    async (operation) => {
      let complete!: (value: unknown) => void;
      const endpoint = {
        dispatch: vi.fn(
          () =>
            new Promise((resolve) => {
              complete = resolve;
            }),
        ),
      };
      const adapter = createPyWebViewPlatform(endpoint, media());
      const controller = new AbortController();
      const pending =
        operation === 'file'
          ? adapter.selectFile(controller.signal)
          : operation === 'folder'
            ? adapter.selectFolder(controller.signal)
            : adapter.save('fixture', 'fixture.txt', controller.signal);
      controller.abort();
      complete({
        status: 'ok',
        value:
          operation === 'save'
            ? null
            : { kind: operation, reference: 'fixture' },
      });
      expect(await pending).toEqual({ status: 'cancelled' });
      expect(await adapter.selectFile(controller.signal)).toEqual({
        status: 'cancelled',
      });
      expect(endpoint.dispatch).toHaveBeenCalledTimes(1);
    },
  );

  it('uses only the typed native endpoint and refuses path-shaped responses', async () => {
    const endpoint = {
      dispatch: vi.fn().mockResolvedValue({
        status: 'ok',
        value: { kind: 'file', reference: 'fixture' },
      }),
    };
    const adapter = createPyWebViewPlatform(endpoint, media());
    expect(await adapter.selectFile()).toEqual({
      status: 'ok',
      value: { kind: 'file', reference: 'fixture' },
    });
    endpoint.dispatch.mockResolvedValue({
      status: 'ok',
      value: { kind: 'file', reference: 'C:\\private' },
    });
    expect(await adapter.selectFile()).toMatchObject({
      status: 'unavailable',
      reason: 'invalid_native_response',
    });
    endpoint.dispatch.mockResolvedValue({
      status: 'unavailable',
      reason: 'native_proof_required',
    });
    expect(await adapter.readClipboard()).toMatchObject({
      status: 'unavailable',
    });
  });

  it('reports native revocation without falling back to another computer', async () => {
    const endpoint = {
      dispatch: vi.fn().mockRejectedValue(new Error('private sentinel')),
    };
    const transport = media();
    const adapter = createPyWebViewPlatform(endpoint, transport);
    expect(await adapter.save('fixture', 'fixture.txt')).toEqual({
      status: 'unavailable',
      reason: 'native_operation_failed',
    });
    expect(transport.download).not.toHaveBeenCalled();
    expect(await adapter.managedWindow('/api/launcher-shutdown')).toMatchObject(
      { status: 'unavailable' },
    );
  });

  it('provides the identical fake interface with deterministic results and no effects', async () => {
    const adapter = createFakePlatform({
      readClipboard: { status: 'ok', value: 'fixture' },
    });
    expect(await adapter.readClipboard()).toEqual({
      status: 'ok',
      value: 'fixture',
    });
    expect(await adapter.selectFolder()).toEqual({
      status: 'unavailable',
      reason: 'unsupported',
    });
    expect(adapter.calls).toEqual(['readClipboard', 'selectFolder']);
  });
});
