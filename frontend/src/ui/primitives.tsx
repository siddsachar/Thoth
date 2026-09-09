import {
  forwardRef,
  useEffect,
  useRef,
  useState,
  type ButtonHTMLAttributes,
  type InputHTMLAttributes,
  type ReactNode,
  type RefObject,
  type SelectHTMLAttributes,
} from 'react';
import * as Tooltip from '@radix-ui/react-tooltip';
import * as Dropdown from '@radix-ui/react-dropdown-menu';
import * as Popover from '@radix-ui/react-popover';
import * as TabsPrimitive from '@radix-ui/react-tabs';
import { ChevronDown, AlertCircle, Info } from 'lucide-react';

export const Button = forwardRef<
  HTMLButtonElement,
  ButtonHTMLAttributes<HTMLButtonElement> & {
    variant?: 'primary' | 'secondary' | 'ghost' | 'danger';
    iconOnly?: boolean;
  }
>(function Button(
  {
    variant = 'secondary',
    iconOnly,
    className = '',
    type = 'button',
    ...props
  },
  ref,
) {
  const pointerFocusPrevented = useRef(false);
  return (
    <button
      ref={ref}
      type={type}
      className={`button ${variant} ${iconOnly ? 'icon-button' : ''} ${className}`}
      {...props}
      onPointerDown={(event) => {
        props.onPointerDown?.(event);
        pointerFocusPrevented.current = event.defaultPrevented;
        // WebKit does not focus pointer-clicked buttons. Record a real opener
        // before imperative dialogs run, respecting Radix's focus decisions.
        if (event.button === 0 && !event.defaultPrevented && !props.disabled)
          event.currentTarget.focus({ preventScroll: true });
      }}
      onClick={(event) => {
        // WebKit's native mousedown can blur the pointerdown focus. Capture
        // the opener at click time before an imperative overlay opens.
        if (
          event.detail > 0 &&
          event.button === 0 &&
          !pointerFocusPrevented.current &&
          !props.disabled
        )
          event.currentTarget.focus({ preventScroll: true });
        props.onClick?.(event);
      }}
    />
  );
});
export const Input = forwardRef<
  HTMLInputElement,
  InputHTMLAttributes<HTMLInputElement>
>(function Input({ className = '', ...props }, ref) {
  return <input ref={ref} className={`input ${className}`} {...props} />;
});
export function Select(props: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select {...props} className={`input select ${props.className ?? ''}`} />
  );
}
export function Field({
  label,
  children,
  hint,
}: {
  label: string;
  children: ReactNode;
  hint?: string;
}) {
  return (
    <label className="field">
      <span>{label}</span>
      {children}
      {hint && <small>{hint}</small>}
    </label>
  );
}
export function Hint({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  return (
    <Tooltip.Provider delayDuration={400}>
      <Tooltip.Root>
        <Tooltip.Trigger asChild>{children}</Tooltip.Trigger>
        <Tooltip.Portal>
          <div className="tooltip-layer">
            <Tooltip.Content
              className="tooltip"
              sideOffset={6}
              collisionPadding={12}
            >
              {label}
            </Tooltip.Content>
          </div>
        </Tooltip.Portal>
      </Tooltip.Root>
    </Tooltip.Provider>
  );
}
export type MenuAction = {
  label: string;
  onSelect: (opener: HTMLButtonElement | null) => void;
  disabled?: boolean;
  danger?: boolean;
};
export function Menu({
  label,
  actions,
  children,
  triggerRef,
  focusAfterClose,
}: {
  label: string;
  actions: MenuAction[];
  children?: ReactNode;
  triggerRef?: RefObject<HTMLButtonElement | null>;
  focusAfterClose?: () => HTMLElement | null;
}) {
  const opener = useRef<HTMLButtonElement>(null);
  return (
    <Dropdown.Root>
      <Dropdown.Trigger asChild>
        <Button
          ref={(element) => {
            opener.current = element;
            if (triggerRef) triggerRef.current = element;
          }}
          aria-label={label}
        >
          {children ?? label}
          <ChevronDown size={16} aria-hidden />
        </Button>
      </Dropdown.Trigger>
      <Dropdown.Portal>
        <Dropdown.Content
          className="menu surface-effect"
          sideOffset={6}
          collisionPadding={12}
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            const target = focusAfterClose?.() ?? opener.current;
            if (target?.isConnected) target.focus({ preventScroll: true });
          }}
        >
          {actions.map((action) => (
            <Dropdown.Item
              key={action.label}
              className={`menu-item ${action.danger ? 'danger-text' : ''}`}
              disabled={action.disabled}
              onSelect={() => {
                // A modal menu can trap focus until it unmounts. Pass the
                // connected trigger explicitly to any task opened by an item.
                action.onSelect(opener.current);
              }}
            >
              {action.label}
            </Dropdown.Item>
          ))}
        </Dropdown.Content>
      </Dropdown.Portal>
    </Dropdown.Root>
  );
}
export function Popup({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  return (
    <Popover.Root>
      <Popover.Trigger asChild>
        <Button>{label}</Button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          aria-label={label}
          className="popover surface-effect"
          sideOffset={8}
          collisionPadding={12}
        >
          {children}
          <Popover.Close asChild>
            <Button>Close {label.toLowerCase()}</Button>
          </Popover.Close>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}
export function Tabs({
  label,
  value,
  onChange,
  items,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  items: { id: string; label: string; content: ReactNode }[];
}) {
  return (
    <TabsPrimitive.Root value={value} onValueChange={onChange}>
      <TabsPrimitive.List className="tabs" aria-label={label}>
        {items.map((item) => (
          <TabsPrimitive.Trigger className="tab" key={item.id} value={item.id}>
            {item.label}
          </TabsPrimitive.Trigger>
        ))}
      </TabsPrimitive.List>
      {items.map((item) => (
        <TabsPrimitive.Content
          key={item.id}
          value={item.id}
          className="tab-content"
        >
          {item.content}
        </TabsPrimitive.Content>
      ))}
    </TabsPrimitive.Root>
  );
}
export function EmptyState({
  title,
  children,
  action,
}: {
  title: string;
  children: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="empty-state">
      <Info size={24} aria-hidden />
      <h2>{title}</h2>
      <p>{children}</p>
      {action}
    </div>
  );
}
export function ErrorState({
  title,
  children,
  action,
}: {
  title: string;
  children: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="state-message" role="alert">
      <AlertCircle size={20} aria-hidden />
      <div>
        <strong>{title}</strong>
        <p>{children}</p>
        {action}
      </div>
    </div>
  );
}
export function Skeleton({ label = 'Loading' }: { label?: string }) {
  const [visible, setVisible] = useState(false);
  useEffect(() => {
    const timer = setTimeout(() => setVisible(true), 150);
    return () => clearTimeout(timer);
  }, []);
  if (!visible)
    return (
      <span role="status" className="visually-hidden">
        {label}
      </span>
    );
  return (
    <div
      className="skeleton-group"
      role="status"
      aria-label={label}
      aria-busy="true"
    >
      <span className="visually-hidden">{label}</span>
      <div aria-hidden className="skeleton" />
      <div aria-hidden className="skeleton short" />
    </div>
  );
}
export function Progress({ label, value }: { label: string; value?: number }) {
  return (
    <label className="field">
      {label}
      <progress aria-label={label} max={100} value={value} />
    </label>
  );
}
export function Surface({
  children,
  elevated = false,
}: {
  children: ReactNode;
  elevated?: boolean;
}) {
  return (
    <section className={`surface ${elevated ? 'surface-effect' : ''}`}>
      {children}
    </section>
  );
}
