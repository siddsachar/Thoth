---
name: developer_coding
display_name: Developer Coding
icon: "code"
description: Structured workflow for implementing code changes in Developer Studio.
enabled_by_default: false
version: "1.0"
tools:
  - developer
tags:
  - developer
  - coding
author: Thoth
---

# Developer Coding

Use this skill when the user asks Developer Studio to add, fix, refactor, or test code in a repository.

Work in this order:

1. Confirm the active workspace, branch, approval mode, and dirty state.
2. Create or update a concise todo plan for anything beyond a tiny edit.
3. Read the smallest set of files that explains the behavior.
4. Make surgical edits with Developer-native write or patch tools so the Inspector can track them.
5. Respect execution mode. In Docker Sandbox mode, edits and commands create pending sandbox changes; import them into the real repo only after the user approves.
6. Run focused tests first. Broaden test scope when the change touches shared behavior.
7. Review the final diff and explain what changed, what passed, and what remains unverified.

Keep progress visible. Before a long inspection, edit, or test cluster, write a short sentence telling the user what you are doing.
