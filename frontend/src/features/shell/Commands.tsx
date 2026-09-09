import { useState } from 'react';
import { Button, EmptyState, Field, Input } from '../../ui/primitives';
export type WorkspaceCommand = {
  label: string;
  keywords?: string;
  run: () => void;
};
export default function Commands({
  commands,
}: {
  commands: WorkspaceCommand[];
}) {
  const [query, setQuery] = useState('');
  const matches = commands.filter((command) =>
    `${command.label} ${command.keywords ?? ''}`
      .toLowerCase()
      .includes(query.trim().toLowerCase()),
  );
  return (
    <div className="stack">
      <Field label="Find a workspace command">
        <Input
          data-initial-focus
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Search commands"
          onKeyDown={(event) => {
            if (event.nativeEvent.isComposing) return;
            if (event.key === 'Enter' && matches.length === 1) {
              event.preventDefault();
              matches[0].run();
            }
          }}
        />
      </Field>
      <ul className="command-list" aria-label="Workspace commands">
        {matches.map((command) => (
          <li key={command.label}>
            <Button variant="ghost" onClick={command.run}>
              {command.label}
            </Button>
          </li>
        ))}
      </ul>
      {matches.length === 0 && (
        <EmptyState title="No matching commands">
          Try a shorter search.
        </EmptyState>
      )}
    </div>
  );
}
