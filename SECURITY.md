# Security Policy

## Reporting a vulnerability

Please do not open a public GitHub issue for security problems.

Email reports to: siddsachar@gmail.com

Include as much detail as you can safely share:

- Affected version or commit
- Operating system
- Steps to reproduce
- Impact and severity estimate
- Logs, screenshots, or proof-of-concept code if relevant

I will try to acknowledge reports within 7 days. If the issue is valid, I will
coordinate a fix, publish a security release when needed, and credit reporters
unless they prefer to stay anonymous.

## Scope

Security-sensitive areas include:

- Shell execution and approval gates
- Browser automation
- Prompt-injection defenses
- Local file access and workspace isolation
- Auto-update manifest, SHA256 verification, and code-signature checks
- Channel adapters and webhook/tunnel handling
- API key storage and OAuth flows

## Supported versions

Only the latest stable release is actively supported. Critical fixes may be
backported when practical.
