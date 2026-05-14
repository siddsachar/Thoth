---
name: developer_guide
display_name: Developer Guide
icon: "code"
description: Guidance for using Developer Studio tools to inspect, edit, test, and review code workspaces safely.
tools:
  - developer
tags:
  - developer
  - code
---

# Developer Tool Guide

Use the Developer tools whenever the active thread is a Developer Studio code thread. They are scoped to the selected workspace, follow the workspace approval mode, and keep the Developer Inspector in sync.

## Default Workflow

1. Start with `developer_workspace_info` for repo, branch, approval mode, execution mode, dirty state, and available context.
2. Use `developer_update_todos` for multi-step coding work. Keep the todo list current as you inspect, edit, test, and review.
3. Inspect with `developer_list_files`, `developer_read_file`, `developer_search`, `developer_git_status`, and `developer_get_diff` before changing code.
4. Prefer `developer_write_file` for creating or replacing full files and `developer_apply_patch` for precise diffs. Both record agent-owned change sets for the Inspector.
5. Use `developer_create_branch`, `developer_switch_branch`, `developer_commit_changes`, `developer_push_current_branch`, and `developer_fast_forward_merge` for common Git workflows instead of raw shell.
6. Use `developer_run_detected_test` for known project test/lint commands. Use `developer_run_command` for custom shell commands only when it is the right tool for the task.
7. If a Developer edit/test/command tool reports `execution_mode: "docker"` and a `sandbox_pending_change_id`, tell the user that the real repo has not changed yet. The Docker container persists for the workspace, but host files still change only through `developer_import_sandbox_changes`.
8. Before finishing, check `developer_get_diff`, run the relevant tests, and summarize files changed, tests run, and any remaining risk.

## Progress Narration

For longer work, narrate briefly in the chat before clusters of tool calls. Good examples:

- "I'll first map the repo and test surface."
- "I found the core module; now I'm adding focused tests."
- "The patch is in. I'm running the detected test command next."

Avoid silent chains of tools when the user would otherwise have no sense of what is happening.

## Shell Use

Shell is allowed in Developer Studio, but use it deliberately:

- Prefer read/search/native Developer tools for repo inspection.
- Use shell for test runs, build commands, git commands, package-manager commands, generated artifacts, and repo-specific scripts.
- For common Git operations, prefer the dedicated Developer Git tools first; use shell for unusual Git flows only.
- Use `developer_run_command` instead of a generic shell tool when possible so Thoth can scope the command to the workspace and record file side effects.
- Match the workspace shell. On Windows/PowerShell, do not use POSIX heredocs such as `python - <<'PY'`; use PowerShell-safe commands or a Developer file tool.
- Do not use shell writes as a workaround for ordinary file edits unless patch/write tools are unsuitable.
- In Docker Sandbox mode, shell commands run in a persistent workspace container backed by a shadow copy. Review the pending sandbox patch before importing it into the real workspace.

## Custom Tools

Use Custom Tools when the user wants to turn a repo or local folder into a reusable Thoth tool.

- Prefer the guided flow: inspect the source first, show the proposed commands, then create/register only after the user accepts the shape.
- If the user asks naturally, e.g. "turn this repo into a tool" or "add this GitHub repo as a tool", handle it with the same inspect -> review -> create -> test -> enable/promote flow.
- Use `custom_tool_builder` for the whole lifecycle when that utility is enabled: action `start` to create a draft, `show`/`list` to inspect drafts, `refine` or `update` to adjust commands, `test` to smoke-test a command, `create` to register, `enable` for Developer use, `promote` for normal chat/plugin tools, and `delete` to remove draft/tool metadata.
- Explain draft commands clearly before action `create`. Use action `refine` for natural-language adjustments instead of creating multiple one-off tools.
- Public repo Custom Tools can be drafted from an explicit repo URL and clone destination; do not add hidden settings or edit internal JSON. Safety comes from command review, test approval/sandboxing, and separate enable/promote steps.
- Do not ask the user to hand-write internal config files. If a repo needs command metadata, inspect it and generate the first config for review.
- Removing a Custom Tool should remove Thoth metadata/plugin registration only; do not delete the source folder unless the user explicitly asks.

## Edit Hygiene

- Make the smallest clear diff that solves the task.
- Preserve unrelated formatting, encoding, notebook metadata, and generated file layout unless changing them is part of the task.
- Avoid whole-file rewrites when a targeted patch will do.
- Use repo-native tests, linters, type checks, or build commands when available.
- For structured files, run cheap validation when practical. For notebooks, JSON parsing is the minimum; use `nbformat` validation if available. Do not execute full notebooks unless the user asks or the repo clearly supports a cheap safe run.
- Do not add language-specific assumptions before inspecting the repo.

## Safety

- Never edit outside the active workspace.
- Respect the workspace approval mode.
- Respect the workspace execution mode. Local mode touches the selected repo directly; Docker Sandbox mode does not touch the selected repo until `developer_import_sandbox_changes` imports an approved patch.
- Do not push, create pull requests, install dependencies, or delete files without explicit approval.
- If the repo is dirty before your work, preserve user changes and mention what you touched.
