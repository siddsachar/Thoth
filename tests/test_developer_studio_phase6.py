from __future__ import annotations

import importlib
import subprocess
import sys


def _fresh_modules(tmp_path, monkeypatch):
    monkeypatch.setenv("THOTH_DATA_DIR", str(tmp_path / "data"))
    sys.modules.pop("developer.github", None)
    import developer.github as github

    return importlib.reload(github)


def test_developer_gh_status_handles_missing_cli(tmp_path, monkeypatch):
    github = _fresh_modules(tmp_path, monkeypatch)
    monkeypatch.setattr(github, "resolve_github_cli", lambda: "")

    status = github.get_gh_status()

    assert status.installed is False
    assert status.authenticated is False
    assert "not installed" in status.message


def test_developer_push_requires_approval_until_user_confirms(tmp_path, monkeypatch):
    github = _fresh_modules(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    calls: list[list[str]] = []

    def fake_run(args, *, cwd=None, timeout=20):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="pushed", stderr="")

    monkeypatch.setattr(github, "_run", fake_run)

    blocked = github.push_current_branch(str(repo), "agent_run")
    allowed = github.push_current_branch(str(repo), "agent_run", confirmed=True)

    assert blocked.ran is False
    assert blocked.decision.decision == "ask"
    assert allowed.ran is True
    assert allowed.ok is True
    assert calls == [["git", "push", "-u", "origin", "HEAD"]]


def test_developer_pr_uses_gh_cli_after_confirmed_approval(tmp_path, monkeypatch):
    github = _fresh_modules(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(github, "resolve_github_cli", lambda: "gh")

    def fake_run(args, *, cwd=None, timeout=20):
        return subprocess.CompletedProcess(
            args,
            0,
            stdout="https://github.com/example/repo/pull/12\n",
            stderr="",
        )

    monkeypatch.setattr(github, "_run", fake_run)

    result = github.create_pull_request(str(repo), "auto_edit", confirmed=True, title="Feature", body="Ready")

    assert result.ran is True
    assert result.ok is True
    assert result.url == "https://github.com/example/repo/pull/12"


def test_developer_pr_preview_uses_branch_and_changed_files(tmp_path, monkeypatch):
    github = _fresh_modules(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()

    def fake_run(args, *, cwd=None, timeout=20):
        if args[:2] == ["git", "branch"]:
            return subprocess.CompletedProcess(args, 0, stdout="feat/cool-change\n", stderr="")
        if args[:2] == ["git", "status"]:
            return subprocess.CompletedProcess(args, 0, stdout=" M README.md\n?? app.py\n", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(github, "_run", fake_run)

    preview = github.suggest_pull_request_text(str(repo))

    assert preview.title == "Cool change"
    assert "`README.md`" in preview.body
    assert "`app.py`" in preview.body
    assert preview.changed_files == 2


def test_developer_pr_is_blocked_in_read_only_even_when_clicked(tmp_path, monkeypatch):
    github = _fresh_modules(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(github, "resolve_github_cli", lambda: "gh")

    result = github.create_pull_request(str(repo), "read_only", confirmed=True)

    assert result.ran is False
    assert result.decision.decision == "block"
