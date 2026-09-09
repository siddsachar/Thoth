import type * as Wire from '../../../contracts/client-platform/v1/typescript/client';

export type * from '../../../contracts/client-platform/v1/typescript/client';

export type ClientError = {
  code: string;
  message: string;
  recovery: 'authenticate' | 'update' | 'retry' | 'review' | 'none';
};
export type ClientStatus =
  | 'loading'
  | 'ready'
  | 'disconnected'
  | 'reconnecting'
  | 'incompatible'
  | 'unauthorized'
  | 'fatal';
/** Client presentation metadata only; this is not an accepted v1 wire event. */
export type PanelDescriptor = {
  panel_kind: string;
  title: string;
  resource_ref?: string;
  resource_kind?: Wire.ResourceBinding['kind'];
  resource_revision?: string;
  subresource_key?: string;
  required_capabilities?: string[];
};
export type ClientPanelSuggestion = {
  type: 'panel.suggested';
  conversation_id: string;
  conversation_revision: string;
  descriptor: PanelDescriptor;
};
export type ClientState = {
  status: ClientStatus;
  error: ClientError | null;
  connection: 'none' | 'sse' | 'poll';
  handshake: Omit<Wire.HandshakeView, 'csrf_token'> | null;
  conversations: Wire.ConversationView[];
  hasMoreConversations: boolean;
  loadingConversations: boolean;
  selectedConversationId: string | null;
  conversation: Wire.ConversationView | null;
  projection: Wire.Snapshot | null;
  hasMoreTranscript: boolean;
  loadingConversation: boolean;
  suggestions: ClientPanelSuggestion[];
  revision: number;
};

export function isPanelDescriptor(value: unknown): value is PanelDescriptor {
  if (!value || typeof value !== 'object') return false;
  const row = value as Partial<PanelDescriptor>;
  return (
    typeof row.panel_kind === 'string' &&
    /^[a-z][a-z0-9.:-]{0,80}$/.test(row.panel_kind) &&
    typeof row.title === 'string' &&
    row.title.length <= 160 &&
    (row.resource_ref === undefined ||
      (typeof row.resource_ref === 'string' &&
        /^[a-zA-Z0-9:_-]{1,256}$/.test(row.resource_ref))) &&
    (row.resource_kind === undefined ||
      ['workspace', 'artifact', 'browser_session', 'task', 'document'].includes(
        row.resource_kind,
      )) &&
    (row.resource_revision === undefined ||
      (typeof row.resource_revision === 'string' &&
        /^(0|[1-9][0-9]{0,19})$/.test(row.resource_revision))) &&
    (row.subresource_key === undefined ||
      (typeof row.subresource_key === 'string' &&
        row.subresource_key.length <= 160)) &&
    (row.required_capabilities === undefined ||
      (Array.isArray(row.required_capabilities) &&
        row.required_capabilities.length <= 20 &&
        row.required_capabilities.every(
          (item) => typeof item === 'string' && item.length <= 128,
        )))
  );
}

/** The sole network boundary. Fixtures implement this same interface. */
export interface ClientTransport {
  connect(signal?: AbortSignal): Promise<Wire.HandshakeView>;
  listConversations(
    cursor?: string,
    signal?: AbortSignal,
  ): Promise<Wire.ConversationPage>;
  getConversation(
    id: string,
    signal?: AbortSignal,
  ): Promise<Wire.ConversationView>;
  getTranscript(
    id: string,
    cursor?: string,
    signal?: AbortSignal,
  ): Promise<Wire.TranscriptPage>;
  subscribe(id: string, signal?: AbortSignal): Promise<Wire.SubscriptionView>;
  observe(
    subscription: string,
    cursor: string,
    signal: AbortSignal,
  ): AsyncIterable<Wire.EventRecord | Wire.StreamReset>;
  poll(
    subscription: string,
    cursor: string,
    signal?: AbortSignal,
  ): Promise<Wire.EventPage>;
  acknowledge(
    subscription: string,
    cursor: string,
    signal?: AbortSignal,
  ): Promise<Wire.Acknowledged>;
  unsubscribe(
    subscription: string,
    signal?: AbortSignal,
    keepalive?: boolean,
  ): Promise<Wire.Unsubscribed>;
  command(
    target: string | null,
    command: Wire.Command,
    key: string,
    signal?: AbortSignal,
  ): Promise<Wire.CommandReceipt>;
  receipt(id: string, signal?: AbortSignal): Promise<Wire.CommandReceipt>;
  upload(
    conversation: string,
    file: File,
    signal?: AbortSignal,
  ): Promise<Wire.AttachmentView>;
  download(reference: string, signal?: AbortSignal): Promise<Blob>;
  clearSession(preserveResumeIdentity?: boolean): void;
}
