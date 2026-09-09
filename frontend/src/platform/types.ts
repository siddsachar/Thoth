import type { AttachmentView } from '../api/types';

export type CapabilityResult<T> =
  | { status: 'ok'; value: T }
  | { status: 'cancelled' }
  | { status: 'unavailable'; reason: string };

export type Selection =
  | { kind: 'file'; files: File[] }
  | { kind: 'folder'; files: File[] }
  | { kind: 'file' | 'folder'; reference: string };

export interface PlatformInfo {
  kind: 'browser' | 'pywebview' | 'fake';
  platform: 'browser' | 'windows' | 'macos' | 'linux' | 'unknown';
  capabilities: string[];
}

// Implemented by the authenticated ClientController; never a second transport.
export interface MediaTransport {
  upload(
    conversationId: string,
    file: File,
    signal?: AbortSignal,
  ): Promise<AttachmentView>;
  download(reference: string, signal?: AbortSignal): Promise<Blob>;
}

export interface ClientPlatform {
  discover(): Promise<CapabilityResult<PlatformInfo>>;
  selectFile(signal?: AbortSignal): Promise<CapabilityResult<Selection>>;
  selectFolder(signal?: AbortSignal): Promise<CapabilityResult<Selection>>;
  upload(
    conversationId: string,
    file: File,
    signal?: AbortSignal,
  ): Promise<CapabilityResult<AttachmentView>>;
  readClipboard(): Promise<CapabilityResult<string>>;
  writeClipboard(text: string): Promise<CapabilityResult<null>>;
  openExternal(url: string): Promise<CapabilityResult<null>>;
  managedWindow(route: string): Promise<CapabilityResult<null>>;
  save(
    reference: string,
    name: string,
    signal?: AbortSignal,
  ): Promise<CapabilityResult<null>>;
}

export const unavailable = (
  reason = 'unsupported',
): CapabilityResult<never> => ({ status: 'unavailable', reason });

export function safeExternalUrl(value: string): string | undefined {
  if (
    value.length > 2048 ||
    /[\s\\]/u.test(value) ||
    [...value].some((character) => character.charCodeAt(0) < 32)
  )
    return undefined;
  try {
    const url = new URL(value);
    return ['http:', 'https:'].includes(url.protocol) &&
      url.hostname &&
      !url.username &&
      !url.password
      ? url.href
      : undefined;
  } catch {
    return undefined;
  }
}

export const safeDownloadName = (name: string): boolean =>
  /^[a-zA-Z0-9][a-zA-Z0-9 ._-]{0,119}$/.test(name);

export async function protect<T>(
  action: () => Promise<T>,
): Promise<CapabilityResult<T>> {
  try {
    return { status: 'ok', value: await action() };
  } catch (error) {
    return error instanceof DOMException && error.name === 'AbortError'
      ? { status: 'cancelled' }
      : unavailable('operation_failed');
  }
}
