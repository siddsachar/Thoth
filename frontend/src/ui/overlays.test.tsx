import { fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect, it, vi } from 'vitest';
import { useState } from 'react';
import { OverlayProvider, useOverlay } from './overlays';
import { Button, Input, Skeleton } from './primitives';

function Form({ confirmed }: { confirmed: () => void }) {
  const [draft, setDraft] = useState('Saved example');
  const { open, notify } = useOverlay();
  return (
    <>
      <Input
        aria-label="Draft"
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
      />
      <Button onClick={() => notify('Example notification')}>Notify</Button>
      <Button
        onClick={() =>
          open({
            kind: 'alert',
            title: 'Reset example?',
            description: 'Confirm a local fixture reset.',
            onConfirm: confirmed,
          })
        }
      >
        Reset example
      </Button>
    </>
  );
}
function Fixture({ confirmed }: { confirmed: () => void }) {
  const { open } = useOverlay();
  return (
    <Button
      onClick={() =>
        open({
          title: 'Edit example',
          description: 'An editable local fixture.',
          content: <Form confirmed={confirmed} />,
        })
      }
    >
      Edit
    </Button>
  );
}
it('keeps the originating form mounted while confirmation cancels or confirms', async () => {
  const user = userEvent.setup();
  const confirmed = vi.fn();
  render(
    <OverlayProvider>
      <Fixture confirmed={confirmed} />
    </OverlayProvider>,
  );
  await user.click(screen.getByRole('button', { name: 'Edit' }));
  const input = screen.getByRole('textbox', { name: 'Draft' });
  await user.clear(input);
  await user.type(input, 'Unsaved idea');
  const reset = screen.getByRole('button', {
    name: 'Reset example',
  });
  await user.click(reset);
  expect(screen.getByRole('alertdialog')).toHaveAttribute('aria-modal', 'true');
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Cancel' })).toHaveFocus();
  await user.keyboard('{Escape}');
  expect(confirmed).not.toHaveBeenCalled();
  expect(screen.getByRole('textbox')).toBe(input);
  expect(input).toHaveValue('Unsaved idea');
  expect(reset).toHaveFocus();
  await user.click(reset);
  await user.click(screen.getByRole('button', { name: 'Confirm' }));
  expect(confirmed).toHaveBeenCalledTimes(1);
  expect(screen.getByRole('textbox')).toBe(input);
  await user.click(screen.getByRole('button', { name: 'Close' }));
  expect(screen.getByRole('button', { name: 'Edit' })).toHaveFocus();
});

it('shows a stable loading announcement before delaying skeleton visuals', () => {
  vi.useFakeTimers();
  try {
    const { container, unmount } = render(<Skeleton label="Loading fixture" />);
    expect(screen.getByRole('status')).toHaveTextContent('Loading fixture');
    expect(container.querySelector('.skeleton')).toBeNull();
    // Unmounting cancels the owned delay; it cannot update a discarded surface.
    unmount();
    expect(vi.getTimerCount()).toBe(0);
  } finally {
    vi.useRealTimers();
  }
});

it('does not confirm when Enter originates in unrelated text', async () => {
  const confirmed = vi.fn();
  const user = userEvent.setup();
  render(
    <OverlayProvider>
      <Fixture confirmed={confirmed} />
    </OverlayProvider>,
  );
  await user.click(screen.getByRole('button', { name: 'Edit' }));
  fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Enter' });
  expect(confirmed).not.toHaveBeenCalled();
});

it('queues notifications during a modal so Escape dismisses the active task', async () => {
  const user = userEvent.setup();
  render(
    <OverlayProvider>
      <Fixture confirmed={vi.fn()} />
    </OverlayProvider>,
  );
  const opener = screen.getByRole('button', { name: 'Edit' });
  await user.click(opener);
  await user.click(screen.getByRole('button', { name: 'Notify' }));
  expect(screen.queryByText('Example notification')).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Reset example' }));
  await user.click(screen.getByRole('button', { name: 'Cancel' }));
  await user.keyboard('{Escape}');
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(opener).toHaveFocus();
  expect(await screen.findByText('Example notification')).toBeVisible();
});
