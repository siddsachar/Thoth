import type { ClientError, ClientStatus } from './types';

const descriptions: Record<string, ClientError> = {
  authentication_required: {
    code: 'authentication_required',
    message: 'Connect to Row-Bot to continue.',
    recovery: 'authenticate',
  },
  session_expired: {
    code: 'session_expired',
    message: 'Your connection has expired. Connect again.',
    recovery: 'authenticate',
  },
  action_denied: {
    code: 'action_denied',
    message: 'This action is unavailable for this connection.',
    recovery: 'authenticate',
  },
  protocol_incompatible: {
    code: 'protocol_incompatible',
    message: 'This client needs an update to connect.',
    recovery: 'update',
  },
  revision_conflict: {
    code: 'revision_conflict',
    message: 'This resource changed. Reload and review your action.',
    recovery: 'review',
  },
  idempotency_mismatch: {
    code: 'idempotency_mismatch',
    message:
      'This action identity was already used. Review the original action.',
    recovery: 'review',
  },
  operation_uncertain: {
    code: 'operation_uncertain',
    message:
      'The action outcome is unknown. Check its receipt before trying again.',
    recovery: 'review',
  },
  not_found: {
    code: 'not_found',
    message: 'This resource is no longer available.',
    recovery: 'none',
  },
  cursor_expired: {
    code: 'cursor_expired',
    message: 'This view expired. Reload the current view.',
    recovery: 'retry',
  },
  payload_too_large: {
    code: 'payload_too_large',
    message: 'This item exceeds the supported size.',
    recovery: 'none',
  },
  network_unavailable: {
    code: 'network_unavailable',
    message: 'Disconnected. Your last confirmed view is preserved.',
    recovery: 'retry',
  },
};

/** Never display a server title, arbitrary exception message, path or response body. */
export function clientError(value: unknown): ClientError {
  const candidate =
    value && typeof value === 'object'
      ? (value as { code?: unknown; status?: unknown })
      : {};
  if (candidate.status === 401) return descriptions.session_expired;
  if (candidate.status === 403) return descriptions.action_denied;
  if (candidate.status === 426) return descriptions.protocol_incompatible;
  if (typeof candidate.code === 'string' && descriptions[candidate.code])
    return descriptions[candidate.code];
  if (value instanceof Error && value.message === 'protocol_incompatible')
    return descriptions.protocol_incompatible;
  if (value instanceof TypeError) return descriptions.network_unavailable;
  return {
    code: 'request_failed',
    message: 'Row-Bot could not complete this request.',
    recovery: 'retry',
  };
}

export function failureStatus(error: ClientError): ClientStatus {
  if (error.recovery === 'authenticate') return 'unauthorized';
  if (error.recovery === 'update') return 'incompatible';
  return error.code === 'network_unavailable' ? 'disconnected' : 'fatal';
}

export function aborted(value: unknown): boolean {
  return value instanceof DOMException && value.name === 'AbortError';
}
