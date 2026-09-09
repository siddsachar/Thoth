import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { ClientController } from '../../api/controller';
import { FixtureTransport } from '../../api/fixtures';
import { createFakePlatform } from '../../platform/fake';
import { RuntimeContext } from '../../runtime';
import { OverlayProvider } from '../../ui/overlays';
import Navigation from './Navigation';

const clients: ClientController[] = [];
afterEach(() => {
  clients.splice(0).forEach((controller) => controller.dispose());
  vi.restoreAllMocks();
});

function CurrentRoute() {
  const location = useLocation();
  return (
    <output aria-label="Current route" data-history-key={location.key}>
      {location.pathname}
    </output>
  );
}

async function setup(count = 55, route = '/') {
  const onOpenConversation = vi.fn();
  const transport = new FixtureTransport({ conversationCount: count });
  const list = vi.spyOn(transport, 'listConversations');
  const controller = new ClientController(transport, () => 1);
  clients.push(controller);
  await controller.start();
  render(
    <MemoryRouter initialEntries={[route]}>
      <CurrentRoute />
      <RuntimeContext.Provider
        value={{ controller, platform: createFakePlatform() }}
      >
        <OverlayProvider>
          <Navigation onOpenConversation={onOpenConversation} />
        </OverlayProvider>
      </RuntimeContext.Provider>
    </MemoryRouter>,
  );
  return { controller, transport, list, onOpenConversation };
}

function rows() {
  return within(
    screen.getByRole('list', { name: 'Conversations' }),
  ).getAllByRole('button');
}

it.each(['/primitives', '/settings/appearance'])(
  'returns to the conversation when selecting a row from %s',
  async (route) => {
    const { controller, transport, onOpenConversation } = await setup(2, route);
    expect(screen.getByLabelText('Current route')).toHaveTextContent(route);
    await act(async () => fireEvent.click(rows()[0]));
    expect(screen.getByLabelText('Current route').textContent).toBe('/');
    expect(controller.getSnapshot().selectedConversationId).toBe(
      transport.conversations[0].id,
    );
    expect(transport.counters.commands).toBe(0);
    expect(onOpenConversation).toHaveBeenCalledTimes(1);
  },
);

it('preserves the existing history entry when selecting conversations on the root view', async () => {
  const { controller, transport, onOpenConversation } = await setup(2);
  const historyKey = screen
    .getByLabelText('Current route')
    .getAttribute('data-history-key');
  for (const row of rows()) {
    await act(async () => fireEvent.click(row));
    expect(screen.getByLabelText('Current route')).toHaveAttribute(
      'data-history-key',
      historyKey,
    );
  }
  expect(controller.getSnapshot().selectedConversationId).toBe(
    transport.conversations[1].id,
  );
  expect(transport.counters.commands).toBe(0);
  expect(onOpenConversation).toHaveBeenCalledTimes(2);
});

it('starts with ten ordered rows and expands without fetching or losing cursor access', async () => {
  const { list, transport } = await setup();
  expect(rows()).toHaveLength(10);
  expect(rows().map((row) => row.getAttribute('aria-label'))).toEqual(
    transport.conversations.slice(0, 10).map(({ title }) => title),
  );
  expect(
    screen.queryByRole('button', { name: 'Load more conversations' }),
  ).toBeNull();
  expect(list).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole('button', { name: 'Show more' }));
  expect(rows()).toHaveLength(50);
  expect(list).toHaveBeenCalledTimes(1);
  await act(async () =>
    fireEvent.click(
      screen.getByRole('button', { name: 'Load more conversations' }),
    ),
  );
  expect(rows()).toHaveLength(55);
  expect(list).toHaveBeenCalledTimes(2);
  expect(rows().map((row) => row.getAttribute('aria-label'))).toEqual(
    transport.conversations.map(({ title }) => title),
  );
  expect(
    screen.queryByRole('button', { name: 'Load more conversations' }),
  ).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: 'Show less' }));
  expect(rows()).toHaveLength(10);
  fireEvent.click(screen.getByRole('button', { name: 'Show more' }));
  expect(rows()).toHaveLength(55);
  expect(list).toHaveBeenCalledTimes(2);
  expect(transport.counters.commands).toBe(0);
});

it('retains the selected older row through Show less and section collapse', async () => {
  const { controller, transport } = await setup();
  fireEvent.click(screen.getByRole('button', { name: 'Show more' }));
  await act(async () =>
    fireEvent.click(
      screen.getByRole('button', { name: 'Sample conversation 13' }),
    ),
  );
  expect(controller.getSnapshot().selectedConversationId).toBe(
    'conversation-13',
  );
  fireEvent.click(screen.getByRole('button', { name: 'Show less' }));
  expect(rows()).toHaveLength(11);
  expect(rows().at(-1)).toHaveAttribute('aria-current', 'page');
  const heading = screen.getByRole('button', {
    name: 'Conversations',
  });
  fireEvent.click(heading);
  expect(heading).toHaveAttribute('aria-expanded', 'false');
  expect(
    document.getElementById(heading.getAttribute('aria-controls')!),
  ).not.toBeVisible();
  expect(screen.queryByRole('list', { name: 'Conversations' })).toBeNull();
  const selected = within(
    screen.getByRole('list', { name: 'Current conversation' }),
  ).getAllByRole('button');
  expect(selected).toHaveLength(1);
  expect(selected[0]).toHaveAccessibleName('Sample conversation 13');
  fireEvent.click(heading);
  expect(rows()).toHaveLength(11);
  expect(controller.getSnapshot().selectedConversationId).toBe(
    'conversation-13',
  );
  expect(transport.counters.commands).toBe(0);
});

it('keeps confirmed selection visible when refreshing the list no longer includes it', async () => {
  const { controller, transport } = await setup(15);
  await act(async () => controller.selectConversation('conversation-15'));
  transport.conversations.splice(0);
  await act(async () => controller.loadMoreConversations(true));
  expect(rows()).toHaveLength(1);
  expect(rows()[0]).toHaveAccessibleName('Sample conversation 15');
  expect(rows()[0]).toHaveAttribute('aria-current', 'page');
  expect(screen.queryByText('Your conversations will appear here.')).toBeNull();
});

it('supports an empty collapsible section without introducing commands or controls for nonexistent pages', async () => {
  const { transport } = await setup(0);
  expect(
    screen.getByText('Your conversations will appear here.'),
  ).toBeVisible();
  expect(screen.queryByRole('button', { name: 'Show more' })).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: 'Conversations' }));
  expect(screen.queryByText('Your conversations will appear here.')).toBeNull();
  expect(
    screen.getByRole('link', { name: 'Current application' }),
  ).toHaveAttribute('href', '/');
  expect(transport.counters.commands).toBe(0);
});

it('preserves full accessible titles and safe untitled labels', async () => {
  const { controller, transport } = await setup(2);
  const longTitle = 'A long synthetic conversation title '.repeat(5);
  transport.conversations[0].title = longTitle;
  transport.conversations[1].title = '';
  await act(async () => controller.loadMoreConversations(true));
  expect(rows()[0]).toHaveAttribute('aria-label', longTitle);
  expect(rows()[1]).toHaveAccessibleName('Untitled conversation');
});
