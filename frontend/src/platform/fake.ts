import type { CapabilityResult, ClientPlatform, PlatformInfo } from './types';
import { unavailable } from './types';

export type FakePlatformScript = Partial<{
  [Key in keyof ClientPlatform]: Awaited<ReturnType<ClientPlatform[Key]>>;
}>;

export function createFakePlatform(
  script: FakePlatformScript = {},
): ClientPlatform & { calls: string[] } {
  const calls: string[] = [];
  const result = <T>(
    operation: keyof ClientPlatform,
    fallback: CapabilityResult<T>,
  ): Promise<CapabilityResult<T>> => {
    calls.push(operation);
    return Promise.resolve(
      (script[operation] ?? fallback) as CapabilityResult<T>,
    );
  };
  return {
    calls,
    discover: () =>
      result<PlatformInfo>('discover', {
        status: 'ok',
        value: { kind: 'fake', platform: 'unknown', capabilities: [] },
      }),
    selectFile: () => result('selectFile', unavailable()),
    selectFolder: () => result('selectFolder', unavailable()),
    upload: () => result('upload', unavailable()),
    readClipboard: () => result('readClipboard', unavailable()),
    writeClipboard: () => result('writeClipboard', unavailable()),
    openExternal: () => result('openExternal', unavailable()),
    managedWindow: () => result('managedWindow', unavailable()),
    save: () => result('save', unavailable()),
  };
}
