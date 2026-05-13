---
name: developer_review
display_name: Developer Review
icon: "rule"
description: Code-review workflow for Developer Studio repositories.
enabled_by_default: false
version: "1.0"
tools:
  - developer
tags:
  - developer
  - review
author: Thoth
---

# Developer Review

Use this skill when the user asks for a code review, PR review, or risk assessment in Developer Studio.

Prioritize findings over summaries:

1. Inspect workspace identity, branch, dirty state, and changed files.
2. Read relevant diffs and implementation files.
3. Look for correctness bugs, regressions, data loss, security issues, concurrency hazards, and missing tests.
4. Run or recommend focused tests where useful.
5. Report findings first with file paths and line references when possible. If no issues are found, say so clearly and mention residual risk.

Do not rewrite code during a review unless the user asks for fixes.

