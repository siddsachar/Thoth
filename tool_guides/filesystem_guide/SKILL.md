---
name: filesystem_guide
display_name: Filesystem Guide
icon: "📁"
description: Guidance for workspace file tools vs shell commands.
tools:
  - filesystem
tags: []
---
- FILESYSTEM vs SHELL ROUTING (strict rules):
  * The workspace_* file tools (workspace_read_file, workspace_list_directory,
    workspace_write_file, etc.) ONLY work inside the configured workspace
    folder. They CANNOT access any other location on the computer.
    workspace_read_file also supports image files — it displays them inline
    in chat. To also analyze image contents, use analyze_image with source='file'.
  * For ANY path outside the workspace (D:\, C:\Users, /home, etc.),
    ANY terminal command (python --version, git status, pip install, etc.),
    or ANY system operation: you MUST use run_command. No exceptions.
  * NEVER tell the user to open a terminal or run a command themselves.
    You have run_command — use it directly.
- ORDINARY SAVES AND CONVERSATIONAL CONFIRMATION:
  * Creating, writing, copying, and exporting ordinary workspace files are not
    approval-gated operations. Use the relevant workspace tool directly when
    the user asks to save without requesting a separate confirmation.
  * If the user explicitly says to ask before saving, treat that as normal
    conversation, not a protected-action approval: finish and present the
    material, ask for confirmation, and make zero write/copy/export calls on
    that turn. After a later affirmative user message, make exactly one normal
    write/copy/export call and report success only after the tool succeeds.
  * Do not create an approval request or promise an approval card for an
    ordinary workspace save. Move and delete operations keep their existing
    configured approval behavior.
