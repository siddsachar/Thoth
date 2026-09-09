import type { HandshakeView } from '../api/types';
import { createBrowserPlatform } from './browser';
import type { ClientPlatform, MediaTransport } from './types';

export type {
  CapabilityResult,
  ClientPlatform,
  MediaTransport,
  PlatformInfo,
  Selection,
} from './types';
export { createBrowserPlatform } from './browser';
export { createPyWebViewPlatform } from './native';

export function selectClientPlatform(
  media: MediaTransport,
  handshake: Pick<HandshakeView, 'native_adapter'> | null | undefined,
): ClientPlatform {
  // v1.0's canonical NativeAdapter is Literal[False]. An asserted window flag,
  // viewport or a legacy pywebview.api object cannot override that contract.
  void handshake;
  return createBrowserPlatform(media);
}
