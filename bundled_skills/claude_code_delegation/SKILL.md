---
name: claude_code_delegation
display_name: Claude Code Delegation
icon: "💻"
description: Delegate coding, review, and refactor tasks to Claude Code CLI through Thoth's approval-gated shell workflow.
enabled_by_default: false
version: "1.0"
tags:
  - coding
  - claude
  - delegation
  - review
  - automation
author: Thoth
---

When the user asks to **use Claude Code**, **coordinate Claude Code**, **delegate coding work**, **have another coding agent implement/review/refactor something**, or compares Thoth with Hermes-style Claude Code orchestration, use this workflow.

You are the coordinator. Claude Code is the delegated coding worker. Keep ownership of the user's goal, safety boundaries, summary, follow-up checks, and final explanation.

## Core Principles

1. **Prefer print mode first** — Use `claude -p` for most tasks. It runs one bounded task, returns output, and exits. This works well with Thoth's `run_command` tool and avoids interactive terminal state.
2. **Keep Thoth in charge** — Do not hand off the whole conversation. Give Claude Code a clear task, collect its result, inspect what changed, run appropriate checks, and report back to the user.
3. **Use the Shell tool deliberately** — Use `run_command` for Claude Code CLI calls. Thoth's shell tool persists the working directory per conversation and approval-gates non-safe commands.
4. **Set the project directory explicitly** — Before delegating, confirm or set the repo directory with `pwd` / `Get-Location` and `cd` / `Set-Location`. Claude Code resume and context behaviour depend on the current directory.
5. **Constrain autonomy** — Always use limits such as `--allowedTools`, `--max-turns`, and, when available, `--max-budget-usd` for print-mode tasks.
6. **Avoid permission bypass** — Do not use `--dangerously-skip-permissions` unless the user explicitly asks and understands that Claude Code may edit files and run commands without per-action prompts.
7. **Do not forward secrets** — Never include API keys, tokens, Thoth memory, private notes, or sensitive user data in a Claude Code prompt unless the user explicitly asks for that specific context to be shared.

## Prerequisite Checks

Before the first delegation in a project or session, check whether Claude Code is available and authenticated:

```text
claude --version
claude auth status
claude doctor
```

If these fail, explain the setup path briefly:

```text
npm install -g @anthropic-ai/claude-code
claude auth login
```

On Windows, native print-mode CLI usage may work if `claude` is installed on PATH. For interactive/tmux-style orchestration, recommend WSL2 unless native terminal support has been verified.

## Delegation Workflow

1. **Understand the task** — Restate the requested coding outcome, target repo/path, and expected verification. Ask one clarifying question only if the target or risk level is unclear.
2. **Inspect local state first** — Use safe shell commands such as `git status`, `git diff`, and directory listing commands before delegation. Notice uncommitted user changes and do not overwrite them.
3. **Choose capability scope** — Pick the narrowest Claude Code tool set for the task:
   - Review/explain only: `--allowedTools "Read"`
   - Edit existing files: `--allowedTools "Read,Edit"`
   - Create files/tests: `--allowedTools "Read,Edit,Write"`
   - Run tests/builds: `--allowedTools "Read,Edit,Write,Bash"`
4. **Ask for approval when needed** — A Claude Code run that can edit files or run shell commands is write-capable delegation. If the user did not already authorize that, confirm before running it.
5. **Run Claude Code in print mode** — Use a bounded command with a concise prompt, explicit limits, and JSON output when useful.
6. **Inspect the result** — After Claude Code exits, run `git status` and inspect the diff. Do not assume the delegated agent did the right thing.
7. **Verify** — Run targeted tests, lint, type checks, or smoke checks appropriate to the change. If verification is expensive or unclear, ask before running broad suites.
8. **Report clearly** — Summarize what Claude Code changed, what you verified, any failures or uncertainties, and recommended next steps.

## Print-Mode Command Patterns

Use these as patterns, adapting quoting for the user's shell. In Thoth, it is often cleaner to set the directory first, then run `claude` as a separate command so the shell session cwd persists.

### Status and Setup

```text
claude --version
claude auth status
claude doctor
```

### Read-Only Review

```text
claude -p "Review the current repository changes for bugs, security issues, and missing tests. Do not edit files. Return findings with file paths and verification suggestions." --allowedTools "Read" --max-turns 3 --output-format json
```

### Focused Edit

```text
claude -p "Implement the requested fix in the smallest safe change. Preserve existing style. After editing, summarize changed files and tests to run." --allowedTools "Read,Edit" --max-turns 8 --max-budget-usd 2 --output-format json
```

### Feature Work With Tests

```text
claude -p "Implement this feature and add focused tests. Prefer minimal, idiomatic changes. Run the relevant tests if safe. Return a summary, files changed, tests run, and any remaining risks." --allowedTools "Read,Edit,Write,Bash" --max-turns 12 --max-budget-usd 4 --output-format json
```

### Diff Review

```text
git diff --stat
git diff | claude -p "Review this diff for correctness, regressions, security issues, and missing tests. Do not edit files." --allowedTools "Read" --max-turns 1
```

If the diff is large, save a focused patch file for Claude Code to inspect, but avoid including secrets or unrelated local changes. Remember that Claude Code cannot see Thoth's previous tool output unless you pass the relevant context into the Claude Code prompt, stdin, files, or repository state.

## Prompt Construction

Give Claude Code a compact brief:

```text
Goal: <specific outcome>
Repo/path: <current directory or subpath>
Constraints: preserve user changes, follow existing patterns, avoid broad refactors
Allowed work: <read-only | edit existing files | create tests | run tests>
Verification: <specific commands or ask it to identify tests>
Output: summary, files changed, tests run, remaining risks
```

Do not ask Claude Code to make commits, push branches, publish releases, delete data, rotate secrets, or run production-affecting commands unless the user explicitly requested that operation.

## Session Continuation

When using `--output-format json`, capture the returned `session_id` if Claude Code provides one. Use resume only when continuing the same task in the same project directory:

```text
claude -p "Continue from the previous session and address the remaining test failure." --resume <session_id> --max-turns 5 --allowedTools "Read,Edit,Bash"
```

Use `--continue` only when you are sure the most recent Claude Code session in the current directory is the one you want. If unsure, start a fresh print-mode task instead.

## Worktree Mode

For larger or risky edits, prefer isolating Claude Code's work in a branch or worktree when available:

```text
claude -p "Implement the feature in an isolated worktree and summarize the diff." --worktree thoth-delegation-task --allowedTools "Read,Edit,Write,Bash" --max-turns 12 --max-budget-usd 4
```

Afterward, inspect the worktree diff before merging or copying changes back. Do not merge or delete worktrees without user approval.

## Interactive Mode Is Advanced

Interactive Claude Code is useful for long, multi-turn coding sessions, Claude Code slash commands, and human-in-the-loop exploration. It is also more fragile.

Use interactive mode only when print mode is insufficient. Prefer macOS/Linux/WSL2 with `tmux` for reliable monitoring:

```text
tmux new-session -d -s claude-work -x 140 -y 40
tmux send-keys -t claude-work 'cd /path/to/project; claude' Enter
tmux capture-pane -t claude-work -p -S -50
tmux send-keys -t claude-work 'Now add focused regression tests' Enter
tmux send-keys -t claude-work '/exit' Enter
tmux kill-session -t claude-work
```

Rules for interactive mode:

- Prefer WSL2 on Windows for tmux-based orchestration.
- Monitor with `tmux capture-pane` before assuming the session is stuck.
- Look for a prompt or clear waiting state before sending follow-up input.
- Clean up tmux sessions when done.
- Do not leave background coding agents running silently.

## Safety Boundaries

- Treat Claude Code as a powerful external actor that can edit the repo and spend API quota.
- Use `--allowedTools` every time.
- Use `--max-turns` for print mode every time.
- Use `--max-budget-usd` for non-trivial tasks when available.
- Avoid broad filesystem access with `--add-dir` unless required.
- Avoid `--permission-mode bypassPermissions` and `--dangerously-skip-permissions` by default.
- Do not run destructive git commands, force pushes, deploys, production migrations, or secret-handling tasks through Claude Code without explicit user approval.
- Respect Thoth background workflow safety modes: write-capable Claude Code runs should not happen in unattended background tasks unless the user selected an approval or allow-all mode for that task.

## After Delegation

Always follow up with Thoth-native review:

1. Run `git status`.
2. Inspect changed files or the diff.
3. Run relevant tests/checks if appropriate.
4. Tell the user:
   - what Claude Code attempted,
   - what changed,
   - what passed or failed,
   - what still needs human review.

If Claude Code fails, times out, hits a budget/turn limit, or produces ambiguous output, do not keep retrying blindly. Summarize the failure, narrow the prompt, reduce tool scope if possible, and ask before another write-capable run.