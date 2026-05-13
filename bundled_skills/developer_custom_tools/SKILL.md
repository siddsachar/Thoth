---
name: developer_custom_tools
display_name: Developer Custom Tools
icon: "extension"
description: Workflow for creating, testing, enabling, promoting, and removing Custom Tools from Developer Studio.
enabled_by_default: false
version: "1.0"
tools:
  - developer
tags:
  - developer
  - tools
author: Thoth
---

# Developer Custom Tools

Use this skill when the user asks to add a GitHub repo, local folder, or current workspace as a reusable Thoth Custom Tool.

The user can ask in natural language. Treat requests like "make this repo a tool", "add this GitHub repo as a Custom Tool", or "turn this folder into a tool" as Custom Tool creation requests.

Workflow:

1. Confirm the source: repo URL, local folder, or current workspace. For repo URLs, ask for an explicit clone parent folder if one is not already provided.
2. Start a draft with `custom_tool_builder` action `start` when that utility is enabled. It performs a compact repo scan plus one AI proposal pass, then validates the proposed commands.
3. Explain the draft commands in plain language. Do not ask the user to hand-write internal config files.
4. Refine the draft with action `refine` when the user asks for changes, or action `update` for a direct command/metadata edit.
5. Public repo Custom Tools require explicit user enablement of public repo tools before creation. Do not silently enable that gate.
6. Create only after the user accepts the proposed commands. Use action `create`.
7. Test at least one command with action `test` before enabling or promotion.
8. Enable Developer use with action `enable` when the user wants it active in Developer.
9. Promote with action `promote` only when the user wants the tool available in normal chat/plugin tools.
10. If removing a draft or Custom Tool, use action `delete` and preserve the source folder unless the user explicitly asks to delete files.

Be clear about scope: a Custom Tool starts as Developer-managed metadata around an existing repo/folder; promotion makes it appear in Settings -> Plugins and normal chat tools.
