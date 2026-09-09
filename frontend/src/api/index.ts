import { ClientController } from './controller';
import { HttpTransport } from './http';

export { ClientController, HttpTransport };
export type * from './types';
export { clientError } from './errors';

/** Fixture recordings are a separate development chunk and never load in production. */
export async function createClientController(
  options: {
    base?: string;
    fixture?: 'normal' | 'incompatible' | 'unauthorized' | 'disconnected';
    onFixture?: (transport: import('./fixtures').FixtureTransport) => void;
  } = {},
): Promise<ClientController> {
  if (import.meta.env.VITE_ENABLE_FIXTURES === '1' && options.fixture) {
    const { FixtureTransport } = await import('./fixtures');
    const transport = new FixtureTransport({ scenario: options.fixture });
    options.onFixture?.(transport);
    return new ClientController(transport);
  }
  return new ClientController(new HttpTransport(options.base));
}
