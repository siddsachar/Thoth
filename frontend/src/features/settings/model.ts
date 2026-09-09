/** Navigation metadata only. Each future domain form retains its typed capability API. */
export const settingsGroups = [
  {
    id: 'models',
    label: 'Models and input',
    leaves: ['Providers', 'Models', 'Voice'],
  },
  {
    id: 'knowledge',
    label: 'Knowledge and documents',
    leaves: ['Knowledge', 'Documents'],
  },
  {
    id: 'integrations',
    label: 'Tools and integrations',
    leaves: ['Tools', 'Skills', 'MCP', 'Plugins', 'Accounts', 'Channels'],
  },
  {
    id: 'personal',
    label: 'Personal workspace',
    leaves: ['Buddy', 'Tracker', 'Utilities', 'Preferences'],
  },
  { id: 'system', label: 'System and access', leaves: ['System'] },
] as const;
export const settingsLeaves = settingsGroups.flatMap((group) =>
  group.leaves.map((label) => ({
    id: label.toLowerCase(),
    label,
    category: group.label,
    href: `/settings/${label.toLowerCase()}`,
  })),
);
const aliases: Record<string, string> = {
  cloud: 'providers',
  google: 'accounts',
  gmail: 'accounts',
  calendar: 'accounts',
  migration: 'preferences',
  search: 'tools',
};
export function resolveSetting(value: string) {
  const key = value.toLowerCase();
  return settingsLeaves.find((leaf) => leaf.id === (aliases[key] ?? key));
}
export function searchSettings(query: string) {
  const term = query.trim().toLowerCase();
  return settingsLeaves.filter((leaf) =>
    `${leaf.label} ${leaf.category} ${Object.entries(aliases)
      .filter(([, id]) => id === leaf.id)
      .map(([alias]) => alias)
      .join(' ')}`
      .toLowerCase()
      .includes(term),
  );
}
