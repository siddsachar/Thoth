import { describe, expect, it } from 'vitest';
import {
  resolveSetting,
  searchSettings,
  settingsGroups,
  settingsLeaves,
} from './model';
describe('settings navigation metadata', () => {
  it('has stable unique deep links and canonical groups', () => {
    expect(settingsGroups).toHaveLength(5);
    expect(new Set(settingsLeaves.map((leaf) => leaf.id)).size).toBe(
      settingsLeaves.length,
    );
    for (const leaf of settingsLeaves)
      expect(resolveSetting(leaf.id)?.href).toBe(leaf.href);
  });
  it('resolves legacy labels and searches aliases and categories', () => {
    for (const [alias, target] of [
      ['Cloud', 'providers'],
      ['Google', 'accounts'],
      ['Gmail', 'accounts'],
      ['Calendar', 'accounts'],
      ['Migration', 'preferences'],
      ['Search', 'tools'],
    ])
      expect(resolveSetting(alias)?.id).toBe(target);
    expect(searchSettings('gmail').map((leaf) => leaf.id)).toEqual([
      'accounts',
    ]);
    expect(searchSettings('system and access').map((leaf) => leaf.id)).toEqual([
      'system',
    ]);
    expect(resolveSetting('unknown')).toBeUndefined();
  });
});
