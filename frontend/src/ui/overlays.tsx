import {
  createContext,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import * as Toast from '@radix-ui/react-toast';
import { X } from 'lucide-react';
import { Button } from './primitives';

type Overlay = {
  key?: string;
  title: string;
  description: string;
  content?: ReactNode;
  kind?: 'dialog' | 'sheet' | 'drawer' | 'alert';
  confirmLabel?: string;
  onConfirm?: () => void;
  returnFocusTo?: HTMLElement | null;
};
type Task = Overlay & { opener: HTMLElement | null };
type Notice = { id: number; message: string };
const OverlayContext = createContext<{
  open: (overlay: Overlay) => void;
  close: () => void;
  dismiss: (key: string) => void;
  notify: (message: string) => void;
} | null>(null);

/** A single Radix modal focus/scroll scope; confirmation suspends a mounted task. */
export function OverlayProvider({ children }: { children: ReactNode }) {
  const [task, setTask] = useState<Task | null>(null);
  const [confirmation, setConfirmation] = useState<Task | null>(null);
  const [notices, setNotices] = useState<Notice[]>([]);
  const nextNotice = useRef(1);
  const returningTo = useRef<HTMLElement | null>(null);
  const resumeFocus = useRef<HTMLElement | null>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const current = confirmation ?? task;
  const activeElement = () =>
    document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
  function open(overlay: Overlay) {
    if (overlay.kind === 'alert')
      setConfirmation({
        ...overlay,
        opener: overlay.returnFocusTo ?? activeElement(),
      });
    else {
      setConfirmation(null);
      setTask({
        ...overlay,
        opener: overlay.returnFocusTo ?? task?.opener ?? activeElement(),
      });
    }
  }
  function close() {
    if (confirmation) {
      if (task) resumeFocus.current = confirmation.opener;
      else returningTo.current = confirmation.opener;
      setConfirmation(null);
    } else {
      returningTo.current = task?.opener ?? null;
      setTask(null);
    }
  }
  useEffect(() => {
    if (confirmation) cancelRef.current?.focus();
    else if (resumeFocus.current?.isConnected) {
      resumeFocus.current.focus();
      resumeFocus.current = null;
    }
  }, [confirmation]);
  const notify = (message: string) =>
    setNotices((previous) =>
      previous.some((notice) => notice.message === message)
        ? previous
        : [...previous, { id: nextNotice.current++, message }].slice(-3),
    );
  return (
    <OverlayContext.Provider
      value={{
        open,
        close,
        dismiss: (key) => {
          if (task?.key === key) close();
        },
        notify,
      }}
    >
      <Toast.Provider duration={6000} swipeDirection="right">
        {children}
        <Dialog.Root
          open={Boolean(current)}
          onOpenChange={(value) => {
            if (!value) close();
          }}
        >
          <Dialog.Portal>
            <Dialog.Overlay className="overlay-backdrop" />
            <Dialog.Content
              aria-modal="true"
              role={confirmation ? 'alertdialog' : 'dialog'}
              className={`dialog ${confirmation ? 'alert-dialog' : task?.kind === 'sheet' ? 'sheet' : task?.kind === 'drawer' ? 'drawer' : ''}`}
              onOpenAutoFocus={(event) => {
                const search = document.querySelector<HTMLElement>(
                  '[role="dialog"] [data-initial-focus]',
                );
                if (confirmation) {
                  event.preventDefault();
                  cancelRef.current?.focus();
                } else if (search) {
                  event.preventDefault();
                  search.focus();
                }
              }}
              onCloseAutoFocus={(event) => {
                event.preventDefault();
                if (returningTo.current?.isConnected)
                  returningTo.current.focus();
              }}
            >
              <header className="dialog-header">
                <div>
                  <Dialog.Title className="dialog-title">
                    {current?.title}
                  </Dialog.Title>
                  <Dialog.Description className="dialog-description">
                    {current?.description}
                  </Dialog.Description>
                </div>
                {!confirmation && (
                  <Button
                    iconOnly={task?.kind !== 'drawer'}
                    variant="ghost"
                    aria-label={
                      task?.kind === 'drawer'
                        ? 'Back to conversation'
                        : 'Close dialog'
                    }
                    onClick={() => close()}
                  >
                    {task?.kind === 'drawer' ? (
                      'Back'
                    ) : (
                      <X size={20} aria-hidden />
                    )}
                  </Button>
                )}
              </header>
              <div
                className="dialog-body"
                hidden={Boolean(confirmation)}
                inert={Boolean(confirmation)}
              >
                {task?.content}
              </div>
              {confirmation && (
                <div className="dialog-body">{confirmation.content}</div>
              )}
              <footer className="dialog-footer">
                {confirmation ? (
                  <>
                    <Button ref={cancelRef} onClick={close}>
                      Cancel
                    </Button>
                    <Button
                      variant="danger"
                      onClick={() => {
                        confirmation.onConfirm?.();
                        close();
                      }}
                    >
                      {confirmation.confirmLabel ?? 'Confirm'}
                    </Button>
                  </>
                ) : (
                  <Button onClick={close}>Close</Button>
                )}
              </footer>
            </Dialog.Content>
          </Dialog.Portal>
        </Dialog.Root>
        <Toast.Viewport className="toast-viewport" label="Notifications" />
        {!current &&
          notices.map((notice) => (
            <Toast.Root
              className="toast"
              key={notice.id}
              onOpenChange={(value) => {
                if (!value)
                  setNotices((values) =>
                    values.filter((item) => item.id !== notice.id),
                  );
              }}
            >
              <Toast.Description>{notice.message}</Toast.Description>
              <Toast.Close asChild>
                <Button
                  iconOnly
                  variant="ghost"
                  aria-label="Dismiss notification"
                >
                  <X size={16} aria-hidden />
                </Button>
              </Toast.Close>
            </Toast.Root>
          ))}
      </Toast.Provider>
    </OverlayContext.Provider>
  );
}
export function useOverlay() {
  const context = useContext(OverlayContext);
  if (!context) throw new Error('OverlayProvider is required');
  return context;
}
