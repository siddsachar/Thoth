import { afterEach, describe, expect, it, vi } from 'vitest';
import { webcrypto } from 'node:crypto';
import { validateWire } from '../../../contracts/client-platform/v1/typescript/client';
import * as wire from '../../../contracts/client-platform/v1/typescript/client';
import { ClientController } from './controller';
import { HttpTransport } from './http';
import {
  FixtureClock,
  FixtureTransport,
  recorded,
  recordings,
} from './fixtures';
import { clientError } from './errors';
import type {
  Command,
  ConversationView,
  Event,
  EventRecord,
  Snapshot,
  SubscriptionView,
  TranscriptPage,
} from './types';

const clients: ClientController[] = [];
function client(transport = new FixtureTransport()) {
  const value = new ClientController(transport, () => 1);
  clients.push(value);
  return value;
}
async function flush() {
  for (let i = 0; i < 30; i++) await Promise.resolve();
}
afterEach(async () => {
  clients.splice(0).forEach((value) => value.dispose());
  await flush();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('accepted protocol recordings', () => {
  it('consumes every F-P01 through F-P10 recorded response with the canonical validator', () => {
    expect(recordings.map((value) => value.fixture_id)).toEqual(
      Array.from(
        { length: 10 },
        (_, i) => `F-P${String(i + 1).padStart(2, '0')}`,
      ),
    );
    for (const recording of recordings)
      for (const record of recording.records)
        expect(validateWire(record.schema, record.value)).toEqual(record.value);
  });
  it('uses an explicit fake monotonic clock, independent of Windows timer resolution', () => {
    const clock = new FixtureClock();
    const start = clock.now();
    clock.advance(60000);
    expect(clock.now() - start).toBe(60000);
    expect(() => clock.advance(-1)).toThrow();
  });
  it('sanitizes arbitrary server titles, exception messages and paths', () => {
    expect(
      JSON.stringify(
        clientError({ title: '/private/secret', code: '/private/secret' }),
      ),
    ).not.toContain('/private');
    expect(
      JSON.stringify(clientError(new Error('token=synthetic-secret'))),
    ).not.toContain('synthetic-secret');
  });
});

describe('connection and lifecycle ownership', () => {
  it('forwards keepalive only when explicitly requested for HTTP subscription release', async () => {
    const handshake = recorded<wire.HandshakeView>('F-P06', 'HandshakeView')[0];
    vi.spyOn(wire, 'handshake').mockResolvedValue(handshake);
    const unsubscribe = vi
      .spyOn(wire, 'unsubscribe')
      .mockResolvedValue({ unsubscribed: true });
    const transport = new HttpTransport();
    await transport.connect();
    const abort = new AbortController();
    await transport.unsubscribe('ordinary-subscription', abort.signal);
    await transport.unsubscribe('terminal-subscription', undefined, true);
    const proof = {
      client_session_id: handshake.client_session_id,
      csrf_token: handshake.csrf_token,
    };
    expect(unsubscribe.mock.calls).toEqual([
      ['', proof, 'ordinary-subscription', abort.signal, false],
      ['', proof, 'terminal-subscription', undefined, true],
    ]);
  });
  it('keeps ordinary subscription release abortable and terminal release bounded to one keepalive request', async () => {
    const releases: { signal?: AbortSignal; keepalive: boolean }[] = [];
    class ReleaseFixture extends FixtureTransport {
      override unsubscribe(
        subscription: string,
        signal?: AbortSignal,
        keepalive = false,
      ) {
        releases.push({ signal, keepalive });
        return super.unsubscribe(subscription);
      }
    }
    const transport = new ReleaseFixture();
    const value = client(transport);
    await value.start();
    await value.selectConversation('conversation-a');
    await flush();
    await value.selectConversation('conversation-2');
    await flush();
    expect(releases).toHaveLength(1);
    expect(releases[0]?.keepalive).toBe(false);
    expect(releases[0]?.signal?.aborted).toBe(false);
    value.dispose();
    value.dispose();
    await flush();
    expect(releases).toHaveLength(2);
    expect(releases[0]?.signal?.aborted).toBe(true);
    expect(releases[1]).toEqual({ signal: undefined, keepalive: true });
    expect(transport.counters.active).toBe(0);
    expect(transport.counters.streams).toBe(0);
    expect(transport.counters.commands).toBe(0);
  });
  it.each(['synchronous', 'deferred'] as const)(
    'contains %s terminal release failure without retries or command replay',
    async (failure) => {
      let rejectRelease: (error: unknown) => void = () => {};
      let releases = 0;
      class FailedReleaseFixture extends FixtureTransport {
        override unsubscribe(
          subscription: string,
          signal?: AbortSignal,
          keepalive = false,
        ) {
          if (!keepalive) return super.unsubscribe(subscription);
          expect(signal).toBeUndefined();
          releases += 1;
          if (failure === 'synchronous')
            throw new DOMException(
              'Synthetic terminal failure',
              'SecurityError',
            );
          return new Promise<wire.Unsubscribed>((_resolve, reject) => {
            rejectRelease = reject;
          });
        }
      }
      const transport = new FailedReleaseFixture();
      const value = client(transport);
      await value.start();
      await value.selectConversation('conversation-a');
      await flush();
      const confirmed = value.getSnapshot();
      value.dispose();
      rejectRelease(
        new DOMException('Synthetic terminal failure', 'SecurityError'),
      );
      await flush();
      await value.reconnect();
      value.setVisible(true);
      await value.setOnline(true);
      await flush();
      expect(value.getSnapshot()).toBe(confirmed);
      expect(releases).toBe(1);
      expect(transport.counters.streams).toBe(0);
      expect(transport.counters.commands).toBe(0);
    },
  );
  it('retains only an opaque HTTP resume identity while suspended and discards it on revocation', async () => {
    const handshake = recorded<wire.HandshakeView>('F-P06', 'HandshakeView')[0];
    const requests: wire.Handshake[] = [];
    let sessions = 0;
    vi.spyOn(wire, 'handshake').mockImplementation(async (_base, request) => {
      requests.push(request);
      return {
        ...handshake,
        client_session_id:
          request.client_session_id ??
          `00000000-0000-4000-8000-${String(++sessions).padStart(12, '0')}`,
      };
    });
    const transport = new HttpTransport();
    const first = await transport.connect();
    for (let index = 0; index < 101; index += 1) {
      transport.clearSession(true);
      expect(() => transport.receipt('fixture')).toThrow();
      const resumed = await transport.connect();
      expect(resumed.client_session_id).toBe(first.client_session_id);
    }
    expect(sessions).toBe(1);
    expect(
      requests
        .slice(1)
        .every(
          (request) => request.client_session_id === first.client_session_id,
        ),
    ).toBe(true);
    expect(JSON.stringify(requests)).not.toContain('csrf');
    transport.clearSession();
    await transport.connect();
    expect(sessions).toBe(2);
    expect(requests.at(-1)?.client_session_id).toBeUndefined();
  });
  it('does no network work while initially offline and restores latest local selection when online', async () => {
    const transport = new FixtureTransport();
    const value = client(transport);
    await value.setOnline(false);
    await value.start();
    await value.reconnect();
    await value.selectConversation('conversation-3');
    value.setVisible(false);
    value.setVisible(true);
    await value.loadMoreConversations();
    await value.loadMoreTranscript();
    await expect(value.download('fixture')).rejects.toMatchObject({
      code: 'network_unavailable',
    });
    expect(transport.counters.connects).toBe(0);
    expect(transport.counters.subscribes).toBe(0);
    expect(value.getSnapshot().status).toBe('disconnected');
    await Promise.all([value.setOnline(true), value.setOnline(true)]);
    await flush();
    expect(value.getSnapshot().status).toBe('ready');
    expect(value.getSnapshot().selectedConversationId).toBe('conversation-3');
    expect(transport.counters.connects).toBe(1);
    expect(transport.counters.active).toBe(1);
  });
  it('suspends and resumes 101 times without session/lease growth or command replay', async () => {
    vi.stubGlobal('crypto', webcrypto);
    class ResumableFixture extends FixtureTransport {
      sessionCount = 0;
      resumeId: string | undefined;
      override async connect(signal?: AbortSignal) {
        const view = await super.connect(signal);
        this.resumeId ??= `00000000-0000-4000-8000-${String(++this.sessionCount).padStart(12, '0')}`;
        return { ...view, client_session_id: this.resumeId };
      }
      override clearSession(preserve = false) {
        if (!preserve) this.resumeId = undefined;
      }
    }
    const transport = new ResumableFixture();
    const value = client(transport);
    await value.start();
    await value.selectConversation('conversation-a');
    await flush();
    await value.command(
      'conversation-a',
      {
        type: 'conversation.rename',
        command_id: '00000000-0000-4000-8000-000000000016',
        client_session_id: value.getSnapshot().handshake!.client_session_id,
        expected_revision: '1',
        payload: { title: 'Synthetic command before suspension' },
      },
      'before-suspension',
    );
    for (let index = 0; index < 101; index += 1) {
      const projection = value.getSnapshot().projection;
      const calls = {
        connects: transport.counters.connects,
        subscribes: transport.counters.subscribes,
        unsubscribes: transport.counters.unsubscribes,
        polls: transport.counters.polls,
      };
      await value.setOnline(false);
      await flush();
      await value.setOnline(false);
      await value.start();
      await value.reconnect();
      value.setVisible(false);
      value.setVisible(true);
      expect(value.getSnapshot().status).toBe('disconnected');
      expect(value.getSnapshot().handshake).toBeNull();
      expect(value.getSnapshot().projection).toBe(projection);
      expect({
        connects: transport.counters.connects,
        subscribes: transport.counters.subscribes,
        unsubscribes: transport.counters.unsubscribes,
        polls: transport.counters.polls,
      }).toEqual(calls);
      expect(transport.counters.streams).toBe(0);
      await value.setOnline(true);
      await flush();
      expect(transport.sessionCount).toBe(1);
      expect(transport.counters.active).toBe(1);
      expect(transport.counters.streams).toBe(1);
      expect(value.getSnapshot().selectedConversationId).toBe('conversation-a');
    }
    expect(transport.counters.subscribes).toBe(102);
    expect(transport.counters.unsubscribes).toBe(101);
    expect(transport.counters.commands).toBe(1);
    vi.spyOn(transport, 'receipt').mockRejectedValueOnce({
      status: 401,
      code: 'session_expired',
    });
    await expect(value.receipt('expired-fixture')).rejects.toMatchObject({
      code: 'session_expired',
    });
    expect(transport.resumeId).toBeUndefined();
    expect(value.getSnapshot().selectedConversationId).toBeNull();
    await value.reconnect();
    await flush();
    expect(transport.sessionCount).toBe(2);
    expect(transport.counters.commands).toBe(1);
    value.dispose();
    await flush();
    expect(transport.counters.active).toBe(0);
    expect(transport.counters.streams).toBe(0);
  });
  it('fences reversed old bootstraps across repeated offline and online transitions', async () => {
    const transport = new FixtureTransport();
    const value = client(transport);
    let first!: (value: wire.HandshakeView) => void;
    let second!: (reason: unknown) => void;
    vi.spyOn(transport, 'connect')
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            first = resolve;
          }),
      )
      .mockImplementationOnce(
        () =>
          new Promise((_resolve, reject) => {
            second = reject;
          }),
      );
    const original = value.start();
    await value.setOnline(false);
    const middle = value.setOnline(true);
    await value.setOnline(false);
    await value.selectConversation('conversation-3');
    await value.setOnline(true);
    await flush();
    first(recorded<wire.HandshakeView>('F-P06', 'HandshakeView')[0]);
    second({ status: 401, code: 'session_expired' });
    await Promise.all([original, middle]);
    await flush();
    expect(value.getSnapshot().status).toBe('ready');
    expect(value.getSnapshot().selectedConversationId).toBe('conversation-3');
    expect(transport.counters.active).toBe(1);
    expect(transport.counters.streams).toBe(1);
  });
  it.each([1000, 10000] as const)(
    'calibrates %i rows with complete on-demand pages and bounded retained projection',
    async (count) => {
      const transport = new FixtureTransport();
      transport.setTranscriptSize(count);
      const value = client(transport);
      await value.start();
      await value.selectConversation('conversation-a');
      await flush();
      const seen = new Set(
        value.getSnapshot().projection!.rows.map((row) => row.id),
      );
      while (value.getSnapshot().hasMoreTranscript) {
        await value.loadMoreTranscript();
        const state = value.getSnapshot();
        expect(state.status).toBe('ready');
        expect(state.projection!.rows.length).toBeLessThanOrEqual(200);
        state.projection!.rows.forEach((row) => seen.add(row.id));
      }
      expect(seen.size).toBe(count);
      expect(transport.fixtureTranscriptRowCount).toBe(count);
      expect(transport.counters.transcriptPages).toBe(count / 100);
      expect(transport.counters.transcriptRowsDelivered).toBe(count);
    },
  );
  it('emits accepted-shape monotonic text deltas and reconciles the same fixture snapshot', async () => {
    const transport = new FixtureTransport();
    const value = client(transport);
    await value.start();
    await value.selectConversation('conversation-a');
    await flush();
    const before = value.getSnapshot().projection!.projection_revision;
    transport.emitTextDelta('first ');
    await flush();
    transport.emitTextDelta('second');
    await flush();
    const snapshot = value.getSnapshot().projection!;
    expect(snapshot.projection_revision).toBe(String(BigInt(before) + 2n));
    expect(
      snapshot.rows.find((row) => row.id === 'fixture-live-row')?.blocks[0]
        .text,
    ).toBe('first second');
    expect(value.metrics.appliedEvents).toBe(2);
    value.setVisible(false);
    value.setVisible(true);
    await flush();
    expect(value.getSnapshot().projection).toEqual(snapshot);
  });
  it('expires replay exactly once during reconnect and installs a fresh snapshot', async () => {
    const transport = new FixtureTransport();
    const value = client(transport);
    await value.start();
    await value.selectConversation('conversation-a');
    await flush();
    const before = transport.counters.subscribes;
    transport.expireNextReplay();
    await value.reconnect();
    await flush();
    expect(transport.counters.subscribes - before).toBe(2);
    expect(transport.counters.active).toBe(1);
    expect(transport.counters.streams).toBe(1);
    expect(value.getSnapshot().status).toBe('ready');
    expect(value.getSnapshot().projection).not.toBeNull();
    expect(transport.counters.commands).toBe(0);
  });
  it.each(['upload', 'download', 'receipt'] as const)(
    'discards late %s completion after dispose even when transport ignores abort',
    async (operation) => {
      const transport = new FixtureTransport();
      const value = client(transport);
      await value.start();
      let release!: (value: never) => void;
      let observed: AbortSignal | undefined;
      vi.spyOn(transport, operation).mockImplementation(
        (...args: unknown[]) => {
          observed = args.at(-1) as AbortSignal;
          return new Promise<never>((resolve) => {
            release = resolve;
          });
        },
      );
      const pending =
        operation === 'upload'
          ? value.upload('conversation-a', new File(['fixture'], 'fixture.txt'))
          : operation === 'download'
            ? value.download('fixture')
            : value.receipt('fixture');
      const assertion = expect(pending).rejects.toMatchObject({
        name: 'AbortError',
      });
      value.dispose();
      expect(observed?.aborted).toBe(true);
      release({} as never);
      await assertion;
    },
  );
  it('propagates caller cancellation and blocks media after authentication revocation', async () => {
    const transport = new FixtureTransport();
    const value = client(transport);
    await value.start();
    let release!: (value: Blob) => void;
    const download = vi.spyOn(transport, 'download').mockImplementation(
      () =>
        new Promise((resolve) => {
          release = resolve;
        }),
    );
    const caller = new AbortController();
    const pending = value.download('fixture', caller.signal);
    const assertion = expect(pending).rejects.toMatchObject({
      name: 'AbortError',
    });
    caller.abort();
    release(new Blob(['late fixture']));
    await assertion;
    vi.spyOn(transport, 'receipt').mockRejectedValueOnce({
      code: 'session_expired',
      status: 401,
    });
    await expect(value.receipt('fixture')).rejects.toMatchObject({
      code: 'session_expired',
    });
    await expect(value.download('fixture')).rejects.toMatchObject({
      code: 'authentication_required',
    });
    expect(download).toHaveBeenCalledTimes(1);
    expect(value.getSnapshot().status).toBe('unauthorized');
  });
  it('fences a pending media result across authentication loss and a fresh reconnect', async () => {
    const transport = new FixtureTransport();
    const value = client(transport);
    await value.start();
    let release!: (value: Blob) => void;
    vi.spyOn(transport, 'download').mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          release = resolve;
        }),
    );
    const pending = value.download('fixture');
    const assertion = expect(pending).rejects.toMatchObject({
      name: 'AbortError',
    });
    vi.spyOn(transport, 'receipt').mockRejectedValueOnce({
      code: 'session_expired',
      status: 401,
    });
    await expect(value.receipt('fixture')).rejects.toMatchObject({
      code: 'session_expired',
    });
    await value.reconnect();
    release(new Blob(['old session fixture']));
    await assertion;
    expect(value.getSnapshot().status).toBe('ready');
    expect(await value.download('fixture')).toBeInstanceOf(Blob);
  });
  it('handshakes once and never exposes CSRF proof in public state', async () => {
    const transport = new FixtureTransport();
    const value = client(transport);
    await Promise.all([value.start(), value.start()]);
    expect(transport.counters.connects).toBe(1);
    expect(value.getSnapshot().handshake).not.toHaveProperty('csrf_token');
    expect(value.getSnapshot().conversations).toHaveLength(3);
  });
  it.each(['incompatible', 'unauthorized', 'disconnected'] as const)(
    'exposes truthful %s startup recovery',
    async (scenario) => {
      const value = client(new FixtureTransport({ scenario }));
      await value.start();
      expect(value.getSnapshot().status).toBe(scenario);
      expect(value.getSnapshot().handshake).toBeNull();
    },
  );
  it('has zero unchanged notifications during 60s idle and no observers/timers after 100 cycles', async () => {
    vi.useFakeTimers();
    const transport = new FixtureTransport();
    const value = client(transport);
    await value.start();
    await value.selectConversation('conversation-a');
    await flush();
    const notifications = value.metrics.notifications;
    await vi.advanceTimersByTimeAsync(60000);
    transport.clock.advance(60000);
    expect(value.metrics.notifications - notifications).toBe(0);
    for (let i = 0; i < 100; i++) {
      value.setVisible(false);
      await flush();
      value.setVisible(true);
      await flush();
    }
    expect(transport.counters.active).toBe(1);
    expect(transport.counters.streams).toBe(1);
    expect(transport.counters.listeners).toBe(1);
    value.dispose();
    await flush();
    expect(transport.counters.active).toBe(0);
    expect(transport.counters.streams).toBe(0);
    expect(transport.counters.listeners).toBe(0);
    expect(vi.getTimerCount()).toBe(0);
    expect(transport.counters.commands).toBe(0);
  });
  it('switches sequential SSE retries to identical-cursor polling, never concurrently', async () => {
    vi.useFakeTimers();
    class PollFixture extends FixtureTransport {
      override async *observe(): AsyncGenerator<EventRecord> {
        yield* [];
        throw new TypeError('SSE unavailable');
      }
    }
    const transport = new PollFixture();
    const value = client(transport);
    await value.start();
    await value.selectConversation('conversation-a');
    await flush();
    expect(value.getSnapshot().status).toBe('reconnecting');
    await vi.advanceTimersByTimeAsync(3000);
    await flush();
    expect(value.getSnapshot().connection).toBe('poll');
    expect(transport.counters.polls).toBe(1);
    await vi.advanceTimersByTimeAsync(60000);
    expect(transport.counters.polls).toBeLessThanOrEqual(6);
    expect(transport.counters.streams).toBe(0);
    expect(transport.counters.commands).toBe(0);
  });
  it('halts after authentication revocation and clears protected view without replaying commands', async () => {
    vi.useFakeTimers();
    const transport = new FixtureTransport();
    const value = client(transport);
    await value.start();
    await value.selectConversation('conversation-a');
    await flush();
    transport.scenario = 'unauthorized';
    transport.emit({ snapshot_required: true });
    await flush();
    expect(value.getSnapshot().status).toBe('unauthorized');
    expect(value.getSnapshot().projection).toBeNull();
    const calls = transport.counters.subscribes;
    await vi.advanceTimersByTimeAsync(60000);
    expect(transport.counters.subscribes).toBe(calls);
    expect(transport.counters.commands).toBe(0);
  });
});

describe('revisioned snapshot and independent selection', () => {
  it('handles local panel.suggested as an advisory without selecting or requesting data', async () => {
    const transport = new FixtureTransport();
    const value = client(transport);
    await value.start();
    await value.selectConversation('conversation-a');
    await flush();
    const before = value.getSnapshot();
    const suggestion = {
      type: 'panel.suggested' as const,
      conversation_id: 'conversation-2',
      conversation_revision: '999',
      descriptor: { panel_kind: 'fake.info', title: 'Suggested sample' },
    };
    value.suggestPanel(suggestion);
    value.suggestPanel(suggestion);
    expect(value.getSnapshot().suggestions).toHaveLength(1);
    expect(value.getSnapshot().selectedConversationId).toBe(
      before.selectedConversationId,
    );
    expect(value.getSnapshot().projection).toBe(before.projection);
    expect(transport.counters.commands).toBe(0);
    value.dismissSuggestion(suggestion);
    expect(value.getSnapshot().suggestions).toEqual([]);
    value.suggestPanel({ ...suggestion, conversation_revision: '0' });
    expect(value.getSnapshot().suggestions).toEqual([]);
  });
  it('coalesces reconnect and preserves a newer selection made during handshake', async () => {
    let resume!: () => void;
    class HandshakeBarrier extends FixtureTransport {
      override async connect(signal?: AbortSignal) {
        const response = await super.connect(signal);
        if (this.counters.connects > 1)
          await new Promise<void>((resolve) => {
            resume = resolve;
          });
        return response;
      }
    }
    const transport = new HandshakeBarrier();
    const value = client(transport);
    await value.start();
    await value.selectConversation('conversation-a');
    await flush();
    const first = value.reconnect(),
      second = value.reconnect();
    await flush();
    await value.selectConversation('conversation-3');
    resume();
    await Promise.all([first, second]);
    await flush();
    expect(transport.counters.connects).toBe(2);
    expect(value.getSnapshot().conversation?.id).toBe('conversation-3');
    expect(transport.counters.active).toBe(1);
  });
  it('does not republish a delayed conversation page after authentication revocation', async () => {
    let resume!: (page: import('./types').ConversationPage) => void;
    class PageBarrier extends FixtureTransport {
      override async listConversations(cursor?: string, signal?: AbortSignal) {
        if (!cursor) return super.listConversations(cursor, signal);
        return new Promise<import('./types').ConversationPage>((resolve) => {
          resume = resolve;
        });
      }
    }
    const transport = new PageBarrier({ conversationCount: 60 });
    const value = client(transport);
    await value.start();
    await value.selectConversation('conversation-a');
    await flush();
    const pending = value.loadMoreConversations();
    transport.scenario = 'unauthorized';
    transport.emit({ snapshot_required: true });
    await flush();
    resume({
      items: [
        {
          id: 'private-late',
          revision: '1',
          title: 'Protected delayed item',
          pinned: false,
        },
      ],
      has_more: false,
    });
    await pending;
    expect(value.getSnapshot().status).toBe('unauthorized');
    expect(value.getSnapshot().conversations).toEqual([]);
  });
  it('only installs C after deliberately reversed A-B-C completion', async () => {
    const resolvers = new Map<string, (row: ConversationView) => void>();
    class Delayed extends FixtureTransport {
      override getConversation(id: string): Promise<ConversationView> {
        return new Promise((resolve) => resolvers.set(id, resolve));
      }
    }
    const transport = new Delayed();
    const value = client(transport);
    await value.start();
    const pending = ['conversation-a', 'conversation-2', 'conversation-3'].map(
      (id) => value.selectConversation(id),
    );
    for (const id of ['conversation-3', 'conversation-2', 'conversation-a'])
      resolvers.get(id)!({ id, title: id, pinned: false, revision: '1' });
    await Promise.all(pending);
    await flush();
    expect(value.getSnapshot().selectedConversationId).toBe('conversation-3');
    expect(value.getSnapshot().conversation?.id).toBe('conversation-3');
    expect(transport.counters.active).toBe(1);
  });
  it('keeps two client selections independent', async () => {
    const a = client(),
      b = client();
    await Promise.all([a.start(), b.start()]);
    await a.selectConversation('conversation-a');
    await b.selectConversation('conversation-2');
    await flush();
    expect(a.getSnapshot().selectedConversationId).toBe('conversation-a');
    expect(b.getSnapshot().selectedConversationId).toBe('conversation-2');
  });
  it('traverses all 1005 conversations through continuation without duplicate IDs', async () => {
    const value = client(new FixtureTransport({ conversationCount: 1005 }));
    await value.start();
    while (value.getSnapshot().hasMoreConversations)
      await value.loadMoreConversations();
    expect(
      new Set(value.getSnapshot().conversations.map((row) => row.id)).size,
    ).toBe(1005);
  });
  it('traverses the accepted 1005-row recording with at most 200 materialized rows', async () => {
    const pages = recorded<TranscriptPage>('F-P05', 'TranscriptPage');
    const observed = new Set<string>();
    class History extends FixtureTransport {
      override async getTranscript(
        id: string,
        cursor?: string,
      ): Promise<TranscriptPage> {
        const index = cursor
          ? pages.findIndex((page) => page.next_cursor === cursor) + 1
          : 0;
        const page = { ...pages[index], conversation_id: id };
        page.rows.forEach((row) => observed.add(row.id));
        return page;
      }
    }
    const value = client(new History());
    value.setVisible(false);
    await value.start();
    await value.selectConversation('conversation-a');
    while (value.getSnapshot().hasMoreTranscript) {
      await value.loadMoreTranscript();
      expect(value.getSnapshot().projection!.rows.length).toBeLessThanOrEqual(
        200,
      );
    }
    expect(observed.size).toBe(1005);
  });
});

describe('event order, atomic reset and commands', () => {
  it('processes a valid 300-event polling page in bounded slices preserving approval and final state', async () => {
    vi.useFakeTimers();
    const initial = recorded<SubscriptionView>('F-P03', 'SubscriptionView')[0]
      .snapshot;
    const source = recorded<Event>('F-P03', 'Event')[0];
    const final = recorded<Event>('F-P01', 'Event').find(
      (event) =>
        event.type === 'generation.state' &&
        event.payload.status === 'completed',
    )!;
    let supplied = false;
    class LargePoll extends FixtureTransport {
      override async *observe(): AsyncGenerator<EventRecord> {
        yield* [];
        throw new TypeError('SSE unavailable');
      }
      override async poll(_subscription: string, cursor: string) {
        const events: EventRecord[] = supplied
          ? []
          : Array.from({ length: 300 }, (_, index) => {
              const revision = String(index + 1);
              const event = {
                ...source,
                event_id: `batch-${revision}`,
                projection_revision: revision,
                source_sequence_start: revision,
                source_sequence_end: revision,
                ...(index === 299
                  ? { type: final.type, payload: final.payload }
                  : index === 256
                    ? {
                        type: 'approval.required',
                        payload: { status: 'waiting_approval' },
                      }
                    : {
                        type: 'queue.updated',
                        payload: { submission_ids: [], revision },
                      }),
              } as Event;
              return { event, cursor: `batch-cursor-${revision}` };
            });
        supplied = true;
        return {
          snapshot_required: false,
          events,
          cursor: events.at(-1)?.cursor ?? cursor,
        };
      }
    }
    const transport = new LargePoll();
    transport.setSnapshot(initial);
    const value = client(transport);
    const approvals: string[] = [];
    value.subscribe(() => {
      if (value.getSnapshot().projection?.projection_revision === '257')
        approvals.push('observed');
    });
    await value.start();
    await value.selectConversation('conversation-a');
    await flush();
    await vi.advanceTimersByTimeAsync(3001);
    await flush();
    expect(value.metrics.appliedEvents).toBe(300);
    expect(value.metrics.maxBatch).toBe(256);
    expect(approvals).toEqual(['observed']);
    expect(value.getSnapshot().projection?.generation?.status).toBe(
      'completed',
    );
    expect(transport.counters.acks.at(-1)).toBe('batch-cursor-300');
  });
  it('bounds repeated snapshot reset without progress', async () => {
    class ResetLoop extends FixtureTransport {
      override async *observe() {
        yield {
          snapshot_required: true as const,
          recovery: 'resubscribe' as const,
        };
      }
    }
    const transport = new ResetLoop();
    const value = client(transport);
    await value.start();
    await value.selectConversation('conversation-a');
    await flush();
    expect(value.getSnapshot().status).toBe('incompatible');
    expect(transport.counters.subscribes).toBe(4);
    expect(transport.counters.active).toBe(0);
  });
  it('a late old acknowledgement cannot start a second stream or overwrite new selection', async () => {
    let resume!: () => void;
    let firstAck = true;
    class AckBarrier extends FixtureTransport {
      override async acknowledge(
        subscription: string,
        cursor: string,
        signal?: AbortSignal,
      ) {
        if (firstAck) {
          firstAck = false;
          await new Promise<void>((resolve) => {
            resume = resolve;
          });
          return { acknowledged: true as const };
        }
        return super.acknowledge(subscription, cursor, signal);
      }
    }
    const transport = new AckBarrier();
    const value = client(transport);
    await value.start();
    await value.selectConversation('conversation-a');
    await flush();
    await value.selectConversation('conversation-2');
    await flush();
    resume();
    await flush();
    expect(value.getSnapshot().conversation?.id).toBe('conversation-2');
    expect(transport.counters.maxStreams).toBe(1);
  });
  it('late reset cleanup cannot detach the newer subscription from disposal', async () => {
    let resume!: () => void;
    let firstClose = true;
    class CloseBarrier extends FixtureTransport {
      override async unsubscribe(subscription: string) {
        if (firstClose) {
          firstClose = false;
          await new Promise<void>((resolve) => {
            resume = resolve;
          });
        }
        return super.unsubscribe(subscription);
      }
    }
    const transport = new CloseBarrier();
    const value = client(transport);
    await value.start();
    await value.selectConversation('conversation-a');
    await flush();
    transport.emit({ snapshot_required: true });
    await flush();
    await value.selectConversation('conversation-2');
    await flush();
    resume();
    await flush();
    value.dispose();
    await flush();
    expect(transport.counters.active).toBe(0);
    expect(transport.counters.streams).toBe(0);
  });
  async function observed() {
    const transport = new FixtureTransport();
    const initial = recorded<SubscriptionView>('F-P03', 'SubscriptionView')[0]
      .snapshot;
    transport.setSnapshot(initial);
    const value = client(transport);
    await value.start();
    await value.selectConversation('conversation-a');
    await flush();
    return { transport, value, initial };
  }
  function eventRecord(
    initial: Snapshot,
    revision: number,
    sequence = revision,
  ): EventRecord {
    const event = recorded<Event>('F-P03', 'Event')[0];
    return {
      cursor: `cursor-${revision}`,
      event: {
        ...event,
        event_id: `fixture-event-${revision}`,
        projection_revision: String(revision),
        source_sequence_start: String(sequence),
        source_sequence_end: String(sequence),
        server_epoch: initial.server_epoch,
      },
    };
  }
  it('deduplicates accepted events and never acknowledges a backwards cursor', async () => {
    const { value, transport, initial } = await observed();
    const first = eventRecord(initial, 1),
      second = eventRecord(initial, 2);
    transport.emit(first);
    transport.emit(second);
    transport.emit(first);
    await flush();
    expect(value.metrics.appliedEvents).toBe(2);
    expect(value.metrics.duplicateEvents).toBe(1);
    expect(value.getSnapshot().projection?.cursor).toBe('cursor-2');
    expect(transport.counters.acks.at(-1)).toBe('cursor-2');
  });
  it('resubscribes on a source sequence gap and atomically installs a new snapshot cut', async () => {
    const { value, transport, initial } = await observed();
    transport.emit(eventRecord(initial, 1));
    await flush();
    const fresh = { ...initial, projection_revision: '5', cursor: 'fresh-cut' };
    transport.setSnapshot(fresh);
    transport.emit(eventRecord(initial, 2, 4));
    await flush();
    expect(value.getSnapshot().projection).toEqual(fresh);
    expect(value.metrics.resets).toBe(2);
    expect(transport.counters.acks.at(-1)).toBe('fresh-cut');
    expect(transport.counters.commands).toBe(0);
  });
  it('installs snapshot then suffix on return without changing selection', async () => {
    const { value, transport, initial } = await observed();
    value.setVisible(false);
    await flush();
    const fresh = {
      ...initial,
      server_epoch: 'new-epoch',
      projection_revision: '10',
      cursor: 'epoch-cut',
    };
    transport.setSnapshot(fresh);
    value.setVisible(true);
    await flush();
    transport.emit(eventRecord(fresh, 11));
    await flush();
    expect(value.getSnapshot().projection?.projection_revision).toBe('11');
    expect(value.getSnapshot().projection?.server_epoch).toBe('new-epoch');
    expect(value.getSnapshot().selectedConversationId).toBe('conversation-a');
  });
  it('coalesces duplicate command intent, rejects changed input and never retries response loss', async () => {
    vi.stubGlobal('crypto', webcrypto);
    const transport = new FixtureTransport();
    const value = client(transport);
    await value.start();
    const command: Command = {
      command_id: '00000000-0000-4000-8000-000000000011',
      client_session_id: value.getSnapshot().handshake!.client_session_id,
      type: 'conversation.rename',
      expected_revision: '1',
      payload: { title: 'Synthetic rename' },
    };
    const first = value.command('conversation-a', command, 'same-key');
    const second = value.command('conversation-a', command, 'same-key');
    expect(await first).toEqual(await second);
    expect(transport.counters.commands).toBe(1);
    await expect(
      value.command('conversation-2', command, 'same-key'),
    ).rejects.toMatchObject({ code: 'idempotency_mismatch' });
    vi.spyOn(transport, 'command').mockRejectedValueOnce(
      new TypeError('response lost'),
    );
    await expect(
      value.command(
        'conversation-a',
        { ...command, command_id: '00000000-0000-4000-8000-000000000012' },
        'lost-key',
      ),
    ).rejects.toMatchObject({ code: 'network_unavailable' });
    await value.reconnect();
    expect(transport.counters.commands).toBe(1);
    const lost = {
      ...command,
      command_id: '00000000-0000-4000-8000-000000000012',
    };
    await value.retryCommand('conversation-a', lost, 'lost-key');
    expect(transport.counters.commands).toBe(2);
  });
  it.each(['dispose', 'reconnect'] as const)(
    'does not dispatch delayed command verification across %s',
    async (transition) => {
      let verify!: (value: ArrayBuffer) => void;
      vi.stubGlobal('crypto', {
        subtle: {
          digest: () =>
            new Promise<ArrayBuffer>((resolve) => {
              verify = resolve;
            }),
        },
      });
      const transport = new FixtureTransport();
      const value = client(transport);
      await value.start();
      const command: Command = {
        command_id: '00000000-0000-4000-8000-000000000013',
        client_session_id: value.getSnapshot().handshake!.client_session_id,
        type: 'conversation.rename',
        expected_revision: '1',
        payload: { title: 'Synthetic rename' },
      };
      const pending = value.command(
        'conversation-a',
        command,
        'delayed-verification',
      );
      const assertion = expect(pending).rejects.toMatchObject({
        name: 'AbortError',
      });
      await value[transition]();
      verify(new ArrayBuffer(32));
      await assertion;
      expect(transport.counters.commands).toBe(0);
    },
  );
  it('treats an old dispatched command failure as uncertain without revoking a newer session', async () => {
    vi.stubGlobal('crypto', {
      subtle: { digest: () => Promise.resolve(new ArrayBuffer(32)) },
    });
    const transport = new FixtureTransport();
    let reject!: (reason: unknown) => void;
    vi.spyOn(transport, 'command').mockImplementationOnce(
      () =>
        new Promise((_resolve, failure) => {
          reject = failure;
        }),
    );
    const value = client(transport);
    await value.start();
    const command: Command = {
      command_id: '00000000-0000-4000-8000-000000000014',
      client_session_id: value.getSnapshot().handshake!.client_session_id,
      type: 'conversation.rename',
      expected_revision: '1',
      payload: { title: 'Synthetic rename' },
    };
    const pending = value.command(
      'conversation-a',
      command,
      'old-authentication',
    );
    const assertion = expect(pending).rejects.toMatchObject({
      code: 'operation_uncertain',
    });
    await flush();
    await value.reconnect();
    reject({ code: 'session_expired', status: 401 });
    await assertion;
    expect(value.getSnapshot().status).toBe('ready');
    expect(value.getSnapshot().handshake).not.toBeNull();
  });
});
