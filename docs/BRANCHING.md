# Branching Strategy

Thoth uses trunk-based development with protected `main`.

## Core rules

1. `main` must always be releasable.
2. No direct pushes to `main`.
3. Every change goes through a pull request.
4. CI must pass before merge.
5. Squash-and-merge is the default.

## Branch types

| Branch | Use |
|---|---|
| `feat/<name>` | New user-facing feature |
| `fix/<name>` | Bug fix |
| `docs/<name>` | Documentation change |
| `test/<name>` | Test-only change |
| `chore/<name>` | Tooling, CI, dependency upkeep |
| `hotfix/<version>-<name>` | Urgent patch release work |

## Normal development flow

```bash
git checkout main
git pull --ff-only
git checkout -b feat/my-change
# edit, test, commit
git push -u origin feat/my-change
```

Open a PR against `main`. Wait for CI. Review the diff. Squash-and-merge.

## Hotfix flow

If the latest stable release is broken and `main` already contains unfinished
feature work:

```bash
git fetch --tags
git checkout -b hotfix/v3.17.1-critical-fix v3.17.0
# apply fix, test, open PR
```

After release, cherry-pick the hotfix back to `main` if needed.

## When to add a `develop` branch

Do not add one yet. Add `develop` only when there are multiple parallel
contributors and `main` needs longer stabilization windows. Until then, feature
branches plus protected `main` are safer and simpler.
