import * as wire from '../../../contracts/client-platform/v1/typescript/client';
import type { ClientTransport } from './types';

/** Same-origin generated transport; only the in-memory owner holds the CSRF proof. */
export class HttpTransport implements ClientTransport {
  private proof: wire.SessionProof | undefined;
  private resumeSessionId: string | undefined;
  constructor(private readonly base = '') {
    if (base && new URL(base, location.origin).origin !== location.origin)
      throw new Error('Cross-origin transport is unavailable');
  }

  private session(): wire.SessionProof {
    if (!this.proof) throw { code: 'authentication_required', status: 401 };
    return this.proof;
  }

  async connect(signal?: AbortSignal): Promise<wire.HandshakeView> {
    const view = await wire.handshake(
      this.base,
      {
        protocol_major: 1,
        minimum_minor: 0,
        maximum_minor: 0,
        client_build: 'row-bot-client-v2',
        presentation_features: ['panels', 'responsive'],
        ...(this.proof || this.resumeSessionId
          ? {
              client_session_id:
                this.proof?.client_session_id ?? this.resumeSessionId,
            }
          : {}),
      },
      signal,
    );
    signal?.throwIfAborted();
    this.proof = {
      client_session_id: view.client_session_id,
      csrf_token: view.csrf_token,
    };
    return view;
  }
  listConversations(cursor?: string, signal?: AbortSignal) {
    return wire.listConversations(
      this.base,
      this.session(),
      50,
      cursor,
      signal,
    );
  }
  getConversation(id: string, signal?: AbortSignal) {
    return wire.getConversation(this.base, this.session(), id, signal);
  }
  getTranscript(id: string, cursor?: string, signal?: AbortSignal) {
    return wire.getTranscript(
      this.base,
      this.session(),
      id,
      100,
      cursor,
      signal,
    );
  }
  subscribe(id: string, signal?: AbortSignal) {
    return wire.subscribe(this.base, this.session(), id, signal);
  }
  observe(subscription: string, cursor: string, signal: AbortSignal) {
    return wire.observeEvents(
      this.base,
      this.session(),
      subscription,
      cursor,
      signal,
    );
  }
  poll(subscription: string, cursor: string, signal?: AbortSignal) {
    return wire.poll(this.base, this.session(), subscription, cursor, signal);
  }
  acknowledge(subscription: string, cursor: string, signal?: AbortSignal) {
    return wire.acknowledge(
      this.base,
      this.session(),
      subscription,
      cursor,
      signal,
    );
  }
  unsubscribe(subscription: string, signal?: AbortSignal, keepalive = false) {
    return wire.unsubscribe(
      this.base,
      this.session(),
      subscription,
      signal,
      keepalive,
    );
  }
  command(
    target: string | null,
    command: wire.Command,
    key: string,
    signal?: AbortSignal,
  ) {
    return wire.sendConversationCommand(
      this.base,
      target,
      command,
      this.session(),
      key,
      signal,
    );
  }
  receipt(id: string, signal?: AbortSignal) {
    return wire.getReceipt(this.base, this.session(), id, signal);
  }
  clearSession(preserveResumeIdentity = false) {
    this.resumeSessionId = preserveResumeIdentity
      ? (this.proof?.client_session_id ?? this.resumeSessionId)
      : undefined;
    this.proof = undefined;
  }

  async upload(
    conversation: string,
    file: File,
    signal?: AbortSignal,
  ): Promise<wire.AttachmentView> {
    if (file.size < 1 || file.size > 26214400)
      throw { code: 'payload_too_large' };
    signal?.throwIfAborted();
    const hash = await crypto.subtle.digest(
      'SHA-256',
      await file.arrayBuffer(),
    );
    const sha256 = [...new Uint8Array(hash)]
      .map((byte) => byte.toString(16).padStart(2, '0'))
      .join('');
    const session = await wire.beginUpload(
      this.base,
      this.session(),
      {
        conversation_id: conversation,
        name: file.name,
        size_bytes: file.size,
        sha256,
        batch_id: crypto.randomUUID(),
      },
      signal,
    );
    try {
      for (let offset = 0; offset < file.size; offset += 1048576) {
        await wire.uploadChunk(
          this.base,
          this.session(),
          session.upload_id,
          offset,
          file.slice(offset, offset + 1048576),
          signal,
        );
      }
      return await wire.completeUpload(
        this.base,
        this.session(),
        session.upload_id,
        { command_id: crypto.randomUUID() },
        crypto.randomUUID(),
        signal,
      );
    } catch (error) {
      // Cancel only the staging session created by this call; never replay completion.
      await wire
        .cancelUpload(this.base, this.session(), session.upload_id)
        .catch(() => undefined);
      throw error;
    }
  }
  download(reference: string, signal?: AbortSignal) {
    return wire.readAttachment(this.base, this.session(), reference, signal);
  }
}
