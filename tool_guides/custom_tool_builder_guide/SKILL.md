---
name: custom_tool_builder_guide
display_name: Custom Tool Builder Guide
icon: "extension"
description: Guidance for creating reusable Custom Tools from repos or folders.
tools:
  - custom_tool_builder
tags:
  - custom-tools
  - developer
---

# Custom Tool Builder Guide

Use `custom_tool_builder` when the user asks to turn a GitHub repo, local folder, or current project into a reusable Thoth Custom Tool.

Default flow:
1. Start with `action="start"`.
2. For a repo URL, pass `source_url`. If the user gave a clone parent, pass `fields={"clone_parent": "..."}`. If no clone parent is known, ask for one.
3. Show the proposed tool name, warnings, and commands before creating anything.
4. Use `action="refine"` when the user asks to adjust commands or behavior.
5. Use `action="test"` for a smoke test when requested.
6. Use `action="create"` only after the user accepts the draft.
7. Use `action="enable"` for Developer availability and `action="promote"` only when the user explicitly wants it available in normal chat/workflows.

Rules:
- Do not ask the user to hand-write `thoth-custom-tool.json` or internal config files.
- Use `custom_tool_builder` for lifecycle state: clone/import source, draft, refine, create, enable, promote, delete.
- Shell can help with extra read-only inspection or explicit user-approved command testing when the builder needs more evidence.
- Do not use shell to manually register, enable, promote, delete, or edit Custom Tool metadata.
- Public repo Custom Tools do not have a separate hidden enablement gate. Safety comes from explicit clone destination, command review, test approval/sandboxing, and the separate enable/promote steps.
- Explain that generated commands usually operate on the cloned/local repo files unless a command is clearly network-enabled.
- Removing a Custom Tool should preserve source files unless the user explicitly asks to delete managed files.
