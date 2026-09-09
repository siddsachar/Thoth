import { useState } from 'react';
import { Check, Command, Info } from 'lucide-react';
import {
  Button,
  EmptyState,
  ErrorState,
  Field,
  Hint,
  Input,
  Menu,
  Popup,
  Progress,
  Select,
  Skeleton,
  Surface,
  Tabs,
} from '../../ui/primitives';
import { useOverlay } from '../../ui/overlays';

function ExampleForm() {
  const [name, setName] = useState('A useful idea');
  const { open, notify } = useOverlay();
  return (
    <div className="stack">
      <Field label="Example name">
        <Input value={name} onChange={(event) => setName(event.target.value)} />
      </Field>
      <Menu
        label="Example actions"
        actions={[
          {
            label: 'Keep this idea',
            onSelect: () => notify('Idea kept in this example'),
          },
        ]}
      />
      <Popup label="Dialog help">
        <p>This popover belongs to the active dialog.</p>
      </Popup>
      <Button
        onClick={() =>
          open({
            kind: 'alert',
            title: 'Discard this example?',
            description:
              'This confirms a sample action. No files or conversations will change.',
            confirmLabel: 'Discard example',
            onConfirm: () => notify('Example discarded'),
          })
        }
      >
        Discard example
      </Button>
      {Array.from({ length: 8 }, (_, index) => (
        <p key={index}>
          Sample content {index + 1}. The body scrolls while actions remain
          available.
        </p>
      ))}
    </div>
  );
}
export default function Gallery() {
  const [tab, setTab] = useState('controls');
  const { open, notify } = useOverlay();
  const [command, setCommand] = useState('');
  return (
    <section className="gallery stack" aria-label="Component gallery">
      <div>
        <span className="eyebrow">Shared product system</span>
        <h1>Component gallery</h1>
        <p className="muted">
          The same controls, states and surfaces across your workspace.
        </p>
      </div>
      <Surface>
        <h2>Buttons and inputs</h2>
        <div className="inline-actions">
          <Button variant="primary">
            <Check size={18} aria-hidden />
            Primary action
          </Button>
          <Button>Secondary action</Button>
          <Button variant="ghost">Quiet action</Button>
          <Button variant="danger">Destructive action</Button>
          <Button disabled>Unavailable</Button>
          <Hint label="Helpful detail">
            <Button iconOnly aria-label="Helpful detail">
              <Info size={18} aria-hidden />
            </Button>
          </Hint>
        </div>
        <div className="field-row">
          <Field label="Example input">
            <Input placeholder="Write a short label" />
          </Field>
          <Field label="Example selection">
            <Select defaultValue="first">
              <option value="first">First choice</option>
              <option value="second">Second choice</option>
            </Select>
          </Field>
          <Field label="Unavailable input">
            <Input disabled value="Unavailable" readOnly />
          </Field>
        </div>
      </Surface>
      <Surface>
        <h2>Navigation and floating surfaces</h2>
        <Tabs
          label="Gallery examples"
          value={tab}
          onChange={setTab}
          items={[
            {
              id: 'controls',
              label: 'Controls',
              content: <p>Consistent controls with visible focus states.</p>,
            },
            {
              id: 'states',
              label: 'States',
              content: <p>Keyboard arrows switch these tabs.</p>,
            },
          ]}
        />
        <div className="inline-actions">
          <Menu
            label="Sample menu"
            actions={[
              {
                label: 'First action',
                onSelect: () => notify('First action selected'),
              },
              {
                label: 'Unavailable action',
                onSelect: () => undefined,
                disabled: true,
              },
            ]}
          />
          <Popup label="Sample popover">
            <p>Supporting detail stays close to its control.</p>
          </Popup>
          <Button
            onClick={() =>
              open({
                title: 'Sample dialog',
                description: 'A scrollable example with a persistent form.',
                content: <ExampleForm />,
              })
            }
          >
            Open dialog
          </Button>
          <Button
            onClick={() =>
              open({
                kind: 'sheet',
                title: 'Sample sheet',
                description: 'A compact surface for a focused task.',
                content: <ExampleForm />,
              })
            }
          >
            Open sheet
          </Button>
          <Button
            onClick={() =>
              open({
                kind: 'alert',
                title: 'Confirm sample action?',
                description: 'This is a demonstration. No data will change.',
                confirmLabel: 'Confirm sample',
                onConfirm: () => notify('Sample action confirmed'),
              })
            }
          >
            Open alert dialog
          </Button>
          <Button onClick={() => notify('Your example preference is saved')}>
            Show toast
          </Button>
        </div>
      </Surface>
      <Surface>
        <h2>Command surface</h2>
        <Field label="Find a command">
          <Input
            value={command}
            onChange={(event) => setCommand(event.target.value)}
            placeholder="Search sample commands"
          />
        </Field>
        <ul className="command-list" aria-label="Sample commands">
          {['Open sample dialog', 'Show notification']
            .filter((label) =>
              label.toLowerCase().includes(command.toLowerCase()),
            )
            .map((label) => (
              <li key={label}>
                <Button
                  variant="ghost"
                  onClick={() =>
                    label.startsWith('Open')
                      ? open({
                          title: 'Sample dialog',
                          description: 'Command example',
                          content: <ExampleForm />,
                        })
                      : notify('Command completed')
                  }
                >
                  <Command size={18} aria-hidden />
                  {label}
                </Button>
              </li>
            ))}
        </ul>
      </Surface>
      <Surface>
        <h2>Feedback and progress</h2>
        <div className="status-examples">
          {['info', 'success', 'warning', 'danger'].map((status) => (
            <p key={status} className={`status-example ${status}`}>
              {status}: a clear text label accompanies colour.
            </p>
          ))}
        </div>
        <Progress label="Example progress" value={65} />
        <Skeleton label="Loading example" />
        <ErrorState
          title="Unable to load example"
          action={
            <Button onClick={() => notify('Retry requested')}>Try again</Button>
          }
        >
          A clear explanation and a safe next step.
        </ErrorState>
        <EmptyState title="Nothing here yet">
          Useful context appears here when it becomes available.
        </EmptyState>
      </Surface>
      <Surface>
        <h2>Code, changes and charts</h2>
        <pre className="code-sample">
          <code>
            <span className="syntax-keyword">const</span> message ={' '}
            <span className="syntax-string">'Hello, workspace'</span>;<br />
            <span className="code-comment">// A readable code surface</span>
          </code>
        </pre>
        <div className="diff-sample">
          <p className="diff-remove">− Removed example line</p>
          <p className="diff-add">+ Added example line</p>
          <p className="diff-change">~ Changed example line</p>
        </div>
        <div
          className="chart-example"
          role="img"
          aria-label="Sample bar chart: three, five, two, four, six and three"
        >
          <div className="chart-bars">
            {[3, 5, 2, 4, 6, 3].map((value, index) => (
              <span
                key={index}
                style={{
                  height: `${value * 16}px`,
                  background: `var(--chart-series-${index + 1})`,
                }}
              >
                <span>{value}</span>
              </span>
            ))}
          </div>
          <p>1 · 2 · 3 · 4 · 5 · 6</p>
        </div>
        <div className="artifact-example">
          <div>Sample artifact surface</div>
        </div>
      </Surface>
    </section>
  );
}
