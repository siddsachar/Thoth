import {
  protect,
  safeDownloadName,
  safeExternalUrl,
  unavailable,
} from './types';
import type {
  CapabilityResult,
  ClientPlatform,
  MediaTransport,
  Selection,
} from './types';

export function createBrowserPlatform(
  media: MediaTransport,
  target: Window = window,
): ClientPlatform {
  const document = target.document;
  const navigator = target.navigator;
  const activated = () => navigator.userActivation?.isActive !== false;
  let pendingSelection = false;

  function select(
    kind: 'file' | 'folder',
    signal?: AbortSignal,
  ): Promise<CapabilityResult<Selection>> {
    if (signal?.aborted) return Promise.resolve({ status: 'cancelled' });
    if (!activated())
      return Promise.resolve(unavailable('user_gesture_required'));
    if (pendingSelection)
      return Promise.resolve(unavailable('selection_in_progress'));
    const input = document.createElement('input');
    input.type = 'file';
    if (kind === 'folder') {
      if (!('webkitdirectory' in input))
        return Promise.resolve(unavailable('folder_selection_unsupported'));
      input.webkitdirectory = true;
      input.multiple = true;
    }
    input.hidden = true;
    input.setAttribute(
      'aria-label',
      kind === 'file' ? 'Choose file' : 'Choose folder',
    );
    pendingSelection = true;
    return new Promise((resolve) => {
      let settled = false;
      const finish = (result: CapabilityResult<Selection>) => {
        if (settled) return;
        settled = true;
        pendingSelection = false;
        signal?.removeEventListener('abort', cancel);
        input.remove();
        resolve(result);
      };
      const cancel = () => finish({ status: 'cancelled' });
      signal?.addEventListener('abort', cancel, { once: true });
      input.addEventListener('cancel', cancel, { once: true });
      input.addEventListener(
        'change',
        () => {
          const files = Array.from(input.files ?? []);
          const selection: Selection =
            kind === 'file'
              ? { kind: 'file', files }
              : { kind: 'folder', files };
          finish(
            files.length
              ? { status: 'ok', value: selection }
              : { status: 'cancelled' },
          );
        },
        { once: true },
      );
      document.body.append(input);
      try {
        input.click();
      } catch {
        finish(unavailable('selection_failed'));
      }
    });
  }

  const capabilityList = () => [
    'select_file',
    'upload',
    'open_external',
    'save',
    ...('webkitdirectory' in document.createElement('input')
      ? ['select_folder']
      : []),
    ...(typeof navigator.clipboard?.readText === 'function'
      ? ['clipboard_read']
      : []),
    ...(typeof navigator.clipboard?.writeText === 'function'
      ? ['clipboard_write']
      : []),
  ];
  return {
    discover: async () => ({
      status: 'ok',
      value: {
        kind: 'browser',
        platform: 'browser',
        capabilities: capabilityList(),
      },
    }),
    selectFile: (signal) => select('file', signal),
    selectFolder: (signal) => select('folder', signal),
    upload: (conversationId, file, signal) =>
      protect(() => media.upload(conversationId, file, signal)),
    readClipboard: () =>
      !activated()
        ? Promise.resolve(unavailable('user_gesture_required'))
        : typeof navigator.clipboard?.readText === 'function'
          ? protect(async () => {
              const text = await navigator.clipboard.readText();
              if (text.length > 65536) throw new Error('payload_too_large');
              return text;
            })
          : Promise.resolve(unavailable()),
    writeClipboard: (text) =>
      !activated()
        ? Promise.resolve(unavailable('user_gesture_required'))
        : text.length > 65536
          ? Promise.resolve(unavailable('payload_too_large'))
          : typeof navigator.clipboard?.writeText === 'function'
            ? protect(async () => {
                await navigator.clipboard.writeText(text);
                return null;
              })
            : Promise.resolve(unavailable()),
    openExternal: async (value) => {
      const url = safeExternalUrl(value);
      if (!url) return unavailable('invalid_url');
      if (!activated()) return unavailable('user_gesture_required');
      return protect(async () => {
        const link = document.createElement('a');
        link.href = url;
        link.rel = 'noopener noreferrer';
        link.target = '_blank';
        link.click();
        return null;
      });
    },
    managedWindow: async () => unavailable('managed_windows_require_native'),
    save: (reference, name, signal) => {
      if (!safeDownloadName(name))
        return Promise.resolve(unavailable('invalid_name'));
      if (!activated())
        return Promise.resolve(unavailable('user_gesture_required'));
      return protect(async () => {
        const blob = await media.download(reference, signal);
        if (signal?.aborted) throw new DOMException('Cancelled', 'AbortError');
        const url = URL.createObjectURL(blob);
        try {
          const link = document.createElement('a');
          link.href = url;
          link.download = name;
          document.body.append(link);
          link.click();
          link.remove();
        } finally {
          URL.revokeObjectURL(url);
        }
        return null;
      });
    },
  };
}
