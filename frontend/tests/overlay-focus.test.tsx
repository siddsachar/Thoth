import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { expect, it } from 'vitest';
import { OverlayProvider, useOverlay } from '../src/ui/overlays';
import { Button, Input } from '../src/ui/primitives';

function pointerDown(target: HTMLElement, button = 0): void {
  // Deliberately no native click-focus side effect: WebKit may leave body focused.
  const event = new Event('pointerdown', { bubbles: true, cancelable: true });
  Object.defineProperties(event, {
    button: { value: button },
    pointerId: { value: 1 },
    isPrimary: { value: true },
    pointerType: { value: 'mouse' },
  });
  fireEvent(target, event);
}

function Launcher() {
  const { open } = useOverlay();
  return (
    <Button
      onClick={() =>
        open({
          title: 'Pointer opened task',
          description: 'Synthetic focus-return fixture.',
          content: <Input aria-label="Task draft" />,
        })
      }
    >
      Open task
    </Button>
  );
}

it('returns focus to a primary-pointer opener even when native mousedown blurs it', async () => {
  render(
    <OverlayProvider>
      <Launcher />
    </OverlayProvider>,
  );
  const opener = screen.getByRole('button', { name: 'Open task' });
  expect(opener).not.toHaveFocus();
  pointerDown(opener);
  expect(opener).toHaveFocus();
  fireEvent.mouseDown(opener, { button: 0 });
  opener.blur();
  fireEvent.click(opener, { button: 0, detail: 1 });
  expect(
    await screen.findByRole('dialog', { name: 'Pointer opened task' }),
  ).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: 'Close' }));
  await waitFor(() => expect(opener).toHaveFocus());
});

it('respects a prevented pointer event owned by a compound control', () => {
  render(
    <>
      <Input aria-label="Existing focus" />
      <Button onPointerDown={(event) => event.preventDefault()}>
        Managed trigger
      </Button>
    </>,
  );
  const original = screen.getByRole('textbox', { name: 'Existing focus' });
  original.focus();
  const trigger = screen.getByRole('button', { name: 'Managed trigger' });
  pointerDown(trigger);
  fireEvent.click(trigger, { button: 0, detail: 1 });
  expect(original).toHaveFocus();
});

it('does not steal focus for a secondary pointer button', () => {
  render(
    <>
      <Input aria-label="Existing focus" />
      <Button>Secondary click</Button>
    </>,
  );
  const original = screen.getByRole('textbox', { name: 'Existing focus' });
  original.focus();
  pointerDown(screen.getByRole('button', { name: 'Secondary click' }), 2);
  expect(original).toHaveFocus();
});
