---
name: developer_pr_prep
display_name: Developer PR Prep
icon: "account_tree"
description: Prepare branches, diffs, tests, and GitHub PR text from Developer Studio.
enabled_by_default: false
version: "1.0"
tools:
  - developer
tags:
  - developer
  - github
author: Thoth
---

# Developer PR Prep

Use this skill when the user wants to prepare, push, or open a pull request from Developer Studio.

Workflow:

1. Check workspace identity, branch, remote, dirty state, and GitHub CLI status.
2. Recommend a feature branch when the user is still on a protected or shared branch.
3. Review the diff and line counts.
4. Run the relevant tests or clearly state why they were not run.
5. Draft a PR title/body with summary, changed files, tests, and risk.
6. Treat push and PR creation as explicit user-approved actions.

If GitHub CLI is missing or unauthenticated, explain the shortest install/auth path for the current platform.

