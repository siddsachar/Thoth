# Contributing to Thoth

Thanks for taking an interest in Thoth. This document describes how to set up a
dev environment, the expected workflow for changes, and the bar for getting a
pull request merged.

Thoth is a personal-AI-sovereignty project. The priorities, in order, are:

1. **Privacy** — local-first defaults, no telemetry, no surprise network calls
2. **Reliability** — destructive actions confirm, errors degrade gracefully
3. **Test coverage** — new behavior ships with new tests in `test_suite.py`
4. **Cross-platform** — Windows and macOS are first-class; Linux is best-effort

---

## 1. Quick start

### Prerequisites

- **Python 3.12** (the packaged app and CI both target 3.12)
- **Git**
- **Ollama** (optional but recommended for running the full test suite locally:
  https://ollama.com)
- Windows: PowerShell 5.1 or 7
- macOS: Xcode command-line tools

### Setup

```powershell
# Windows (PowerShell)
git clone https://github.com/siddsachar/Thoth.git
cd Thoth
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

```bash
# macOS / Linux
git clone https://github.com/siddsachar/Thoth.git
cd Thoth
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Run Thoth

```bash
python launcher.py
```

### Run the tests

```bash
python test_suite.py
```

The suite is the single source of truth. Section markers (e.g. `Section 73:
Auto-Update`) make it easy to see what each block covers. The run exits
non-zero on any failure.

---

## 2. Branching & commits

Thoth uses a **trunk-based** model:

- `main` is always releasable.
- Every change goes through a short-lived feature branch and a pull request,
  even from the maintainer.
- No direct pushes to `main`.

### Branch names

Use one of the following prefixes:

- `feat/<slug>` — new feature
- `fix/<slug>` — bug fix
- `docs/<slug>` — documentation only
- `refactor/<slug>` — internal refactor with no behavior change
- `test/<slug>` — adds or fixes tests
- `chore/<slug>` — tooling, deps, CI

Examples: `feat/designer-pdf-export`, `fix/discord-voice-warning`.

### Commit messages

Use [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <short description>

<optional longer body>
```

Types match the branch prefixes above (`feat`, `fix`, `docs`, `refactor`,
`test`, `chore`, plus `perf`, `build`, `ci`).

Example:

```
fix(designer): persist attached files before first turn

The first-draft path was sending the build prompt before references hit
disk, so the agent never saw the uploaded brief.
```

Keep commits focused. If a PR ends up with cleanup noise, squash on merge.

---

## 3. Pull requests

1. Fork or create a feature branch off the latest `main`.
2. Make your change. Add or update tests in `test_suite.py`.
3. Run `python test_suite.py` locally and confirm it passes.
4. Push and open a PR against `main`. The PR template will prompt for the
   relevant info; fill out every section.
5. CI will run on Windows, macOS, and Linux. All checks must be green.
6. A maintainer will review. Address feedback by pushing additional commits
   (do not force-push during review unless asked).
7. Squash-and-merge is the default merge style.

### What gets a PR rejected fast

- New behavior with no test coverage
- Adds telemetry, analytics, or "phone home" logic
- Adds a third-party network dependency without an opt-out
- Touches `docs/index.html` (the marketing site is hand-curated)
- Breaks the test suite on any platform
- Includes secrets, API keys, or personal data
- Is enormous and touches unrelated areas

### What gets a PR through fast

- A linked issue describing the problem
- A short rationale in the PR description
- New tests next to existing ones in the right `test_suite.py` section
- Screenshots or a short clip for UI changes
- Stays under ~500 lines of diff where possible

---

## 4. Code style

Python:

- Type hints on new public functions
- Standard library first, third-party second, local last
- Match the surrounding style; no en-masse reformatting in a feature PR
- No `print(...)` for diagnostics in shipped code; use `logging`

UI (NiceGUI):

- Reuse the shared primitives in `ui/` (`bulk_select`, `confirm`, `skeleton`,
  `timer_utils`, `chat_components`) before reaching for new abstractions
- Match the existing dark-mode-first card aesthetic

---

## 5. Tests

`test_suite.py` is one big runner. When you add a feature, add a new section
or extend the most relevant existing section:

```python
# ── Section N: <Your feature> ────────────────────────────────────────────────
print("\n=== Section N: <Your feature> ===")
# ... assertions, increment PASS/FAIL counters using the helpers above ...
```

Tests should:

- Be deterministic (no real network calls; mock cloud providers)
- Run on Windows, macOS, and Linux
- Not depend on a specific Ollama model being pulled (skip cleanly when not
  available)
- Finish in seconds, not minutes

---

## 6. Reporting issues

Use the issue templates at https://github.com/siddsachar/Thoth/issues/new/choose.

For security issues, **do not open a public issue.** See [SECURITY.md](SECURITY.md).

---

## 7. Code of Conduct

Participation in this project is governed by the [Code of Conduct](CODE_OF_CONDUCT.md).
By contributing, you agree to uphold it.

---

## 8. Licensing

Thoth is released under the [Apache License 2.0](LICENSE). By submitting a
contribution you agree your work is licensed under the same terms. See the
[NOTICE](NOTICE) file for attribution requirements.
