from __future__ import annotations

import importlib
import sys
from types import SimpleNamespace


def _fresh_modules(tmp_path, monkeypatch):
    monkeypatch.setenv("THOTH_DATA_DIR", str(tmp_path / "data"))
    for name in [
        "plugins.registry",
        "plugins.state",
        "plugins.loader",
        "developer.storage",
        "developer.tool_capsules",
        "tools.custom_tool_builder_tool",
    ]:
        sys.modules.pop(name, None)
    import developer.storage as storage
    import developer.tool_capsules as capsules

    importlib.reload(storage)
    return importlib.reload(capsules)


def test_tool_capsules_register_public_sources_without_hidden_gate(tmp_path, monkeypatch):
    capsules = _fresh_modules(tmp_path, monkeypatch)
    install_path = tmp_path / "tool"
    install_path.mkdir()

    capsule = capsules.register_capsule(
        "https://github.com/example/tool",
        name="Example Tool",
        installed_path=str(install_path),
    )

    assert capsule.enabled is False
    assert capsule.community is True
    assert capsules.list_capsules()[0].id == capsule.id


def test_tool_capsules_register_disable_and_remove_in_isolated_state(tmp_path, monkeypatch):
    capsules = _fresh_modules(tmp_path, monkeypatch)
    install_path = tmp_path / "capsule"
    install_path.mkdir()

    capsule = capsules.register_capsule(
        "https://github.com/example/tool",
        name="Example Tool",
        version="1.0.0",
        installed_path=str(install_path),
    )
    enabled = capsules.set_capsule_enabled(capsule.id, True)
    capsules.remove_capsule(capsule.id)

    assert capsule.enabled is False
    assert enabled.enabled is True
    assert capsules.list_capsules() == []


def test_tool_capsules_parse_manifest_commands(tmp_path, monkeypatch):
    capsules = _fresh_modules(tmp_path, monkeypatch)
    install_path = tmp_path / "capsule"
    install_path.mkdir()
    (install_path / "thoth-capsule.json").write_text(
        """
        {
          "name": "Repo Helper",
          "version": "2.1.0",
          "commands": [
            {"name": "Smoke", "command": "python --version", "description": "Check Python"}
          ]
        }
        """,
        encoding="utf-8",
    )

    capsule = capsules.register_capsule(
        "https://github.com/example/repo-helper",
        installed_path=str(install_path),
    )

    assert capsule.name == "Repo Helper"
    assert capsule.version == "2.1.0"
    assert capsule.commands[0]["name"] == "Smoke"
    assert capsule.commands[0]["command"] == "python --version"


def test_tool_capsule_generator_infers_gitignore_repo_commands(tmp_path, monkeypatch):
    capsules = _fresh_modules(tmp_path, monkeypatch)
    install_path = tmp_path / "gitignore"
    install_path.mkdir()
    (install_path / "README.md").write_text("# Gitignore templates\n", encoding="utf-8")
    (install_path / "Python.gitignore").write_text("__pycache__/\n.venv/\n", encoding="utf-8")
    (install_path / "Node.gitignore").write_text("node_modules/\n", encoding="utf-8")

    proposal = capsules.propose_capsule_manifest(
        str(install_path),
        source_url="https://github.com/github/gitignore",
    )
    manifest_path = capsules.write_capsule_manifest(str(install_path), proposal)
    parsed = capsules.parse_capsule_manifest(str(install_path))

    command_names = {command["name"] for command in proposal.commands}

    assert proposal.name == "Gitignore Custom Tool"
    assert "List gitignore templates" in command_names
    assert "Show Python template" in command_names
    assert "Count gitignore templates" in command_names
    assert manifest_path.name == "thoth-custom-tool.json"
    assert parsed["commands"][0]["name"] == proposal.commands[0]["name"]


def test_tool_capsule_generate_and_register_writes_manifest(tmp_path, monkeypatch):
    capsules = _fresh_modules(tmp_path, monkeypatch)
    install_path = tmp_path / "repo-helper"
    install_path.mkdir()
    (install_path / "README.md").write_text("# Helper\n", encoding="utf-8")

    capsule = capsules.generate_and_register_capsule(
        str(install_path),
        source_url="https://github.com/example/repo-helper",
    )

    assert (install_path / "thoth-custom-tool.json").exists()
    assert capsule.name == "Repo Helper Custom Tool"
    assert capsule.commands
    assert capsules.list_capsules()[0].id == capsule.id


def test_custom_tool_generator_infers_tldr_repo_commands(tmp_path, monkeypatch):
    capsules = _fresh_modules(tmp_path, monkeypatch)
    install_path = tmp_path / "tldr"
    (install_path / "pages" / "common").mkdir(parents=True)
    (install_path / "pages" / "common" / "tar.md").write_text("# tar\n\n> Archive utility.\n", encoding="utf-8")
    (install_path / "pages.es" / "common").mkdir(parents=True)
    (install_path / "pages.es" / "common" / "tar.md").write_text("# tar\n", encoding="utf-8")
    (install_path / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    (install_path / "README.md").write_text("# tldr-pages\n", encoding="utf-8")

    proposal = capsules.propose_capsule_manifest(
        str(install_path),
        source_url="https://github.com/tldr-pages/tldr",
    )

    command_names = {command["name"] for command in proposal.commands}

    assert "List TLDR languages" in command_names
    assert "Count TLDR pages" in command_names
    assert "Search TLDR pages" in command_names
    assert "Show tar TLDR page" in command_names
    assert "List gitignore templates" not in command_names


def test_custom_tool_ai_generator_uses_repo_brief_and_validates_commands(tmp_path, monkeypatch):
    capsules = _fresh_modules(tmp_path, monkeypatch)
    install_path = tmp_path / "public-apis"
    install_path.mkdir()
    (install_path / "README.md").write_text(
        "# Public APIs\n\n"
        "## Animals\n\n"
        "| API | Description | Auth | HTTPS | CORS |\n"
        "| Cat Facts | Daily cat facts | No | Yes | Yes |\n",
        encoding="utf-8",
    )

    class _FakeMessage:
        content = """
        {
          "name": "Public APIs Custom Tool",
          "version": "1.0.0",
          "purpose": "Search and summarize the public API catalog.",
          "commands": [
            {
              "name": "Search APIs",
              "description": "Search README rows for a keyword.",
              "command": "python -c \\"from pathlib import Path; q='{query}'.strip().lower() or 'cat'; print('\\\\n'.join(line for line in Path('README.md').read_text(encoding='utf-8', errors='replace').splitlines() if q in line.lower())[:6000])\\""
            },
            {
              "name": "Show quoted descriptions",
              "description": "Show markdown quote lines.",
              "command": "python -c \\"from pathlib import Path; print('\\\\n'.join(line for line in Path('README.md').read_text(encoding='utf-8', errors='replace').splitlines() if line.strip().startswith('>'))[:6000])\\""
            },
            {
              "name": "Format rows safely",
              "description": "Use Python string formatting without shell formatting.",
              "command": "python -c \\"print('{}: {}'.format('Animals', 1))\\""
            },
            {
              "name": "Redirect Bad",
              "description": "Should be rejected.",
              "command": "python -c \\"print('bad')\\" > out.txt"
            },
            {
              "name": "Format Disk Bad",
              "description": "Should be rejected.",
              "command": "format C:"
            },
            {
              "name": "Install Bad",
              "description": "Should be rejected.",
              "command": "pip install requests"
            }
          ],
          "warnings": []
        }
        """

    class _FakeLLM:
        def invoke(self, _messages):
            return _FakeMessage()

    import models

    monkeypatch.setattr(models, "get_current_model", lambda: "fake")
    monkeypatch.setattr(models, "get_llm_for", lambda _model: _FakeLLM())

    proposal = capsules.generate_custom_tool_proposal_with_llm(
        str(install_path),
        source_url="https://github.com/public-apis/public-apis",
    )

    assert proposal.name == "Public APIs Custom Tool"
    assert [command["name"] for command in proposal.commands] == [
        "Search APIs",
        "Show quoted descriptions",
        "Format rows safely",
    ]
    assert "pip install" not in proposal.commands[0]["command"]
    assert any("Redirect Bad" in warning for warning in proposal.warnings)
    assert any("Format Disk Bad" in warning for warning in proposal.warnings)
    assert any("Install Bad" in warning for warning in proposal.warnings)
    assert not any("\\bformat\\b" in warning for warning in proposal.warnings)


def test_custom_tool_public_source_writes_manifest_without_hidden_gate(tmp_path, monkeypatch):
    capsules = _fresh_modules(tmp_path, monkeypatch)
    install_path = tmp_path / "repo-helper"
    install_path.mkdir()
    (install_path / "README.md").write_text("# Helper\n", encoding="utf-8")

    capsule = capsules.generate_and_register_capsule(
        str(install_path),
        source_url="https://github.com/example/repo-helper",
    )

    assert (install_path / "thoth-custom-tool.json").exists()
    assert capsule.enabled is False
    assert capsules.list_capsules()[0].id == capsule.id


def test_custom_tool_local_source_does_not_need_public_gate(tmp_path, monkeypatch):
    capsules = _fresh_modules(tmp_path, monkeypatch)
    install_path = tmp_path / "local-helper"
    install_path.mkdir()
    (install_path / "README.md").write_text("# Local Helper\n", encoding="utf-8")

    capsule = capsules.generate_and_register_capsule(
        str(install_path),
        source_url="local://local-helper",
    )

    assert capsule.name == "Local Helper Custom Tool"
    assert (install_path / "thoth-custom-tool.json").exists()


def test_custom_tool_builder_manages_draft_lifecycle(tmp_path, monkeypatch):
    capsules = _fresh_modules(tmp_path, monkeypatch)
    install_path = tmp_path / "local-helper"
    install_path.mkdir()

    def _fake_proposal(path, *, source_url="", use_ai=False):
        return capsules.CapsuleManifestProposal(
            name="Local Helper Custom Tool",
            version="1.0.0",
            source_url=source_url or "local://helper",
            installed_path=str(install_path),
            commands=[
                {
                    "name": "List files",
                    "command": "python -c \"print('ok')\"",
                    "description": "Smoke command",
                }
            ],
            warnings=[],
        )

    class _FakeResult:
        def __init__(self):
            self.command = "python -c \"print('ok')\""
            self.cwd = str(install_path)
            self.ran = True
            self.ok = True
            self.returncode = 0
            self.stdout = "ok\n"
            self.stderr = ""

    monkeypatch.setattr(capsules, "propose_capsule_manifest", _fake_proposal)
    monkeypatch.setattr(capsules, "run_workspace_command", lambda *_args, **_kwargs: _FakeResult())

    started = capsules.custom_tool_builder("start", source_path=str(install_path), source_url="local://helper")
    draft_id = started["draft"]["id"]
    tested = capsules.custom_tool_builder("test", draft_id=draft_id)
    created = capsules.custom_tool_builder("create", draft_id=draft_id)
    enabled = capsules.custom_tool_builder("enable", draft_id=draft_id)

    assert started["draft"]["name"] == "Local Helper Custom Tool"
    assert tested["result"]["ok"] is True
    assert (install_path / "thoth-custom-tool.json").exists()
    assert created["tool"]["id"] == "local-helper-custom-tool"
    assert enabled["tool"]["enabled"] is True
    assert capsules.get_custom_tool_draft(draft_id).status == "enabled"


def test_custom_tool_builder_starts_from_repo_url_with_clone_parent(tmp_path, monkeypatch):
    capsules = _fresh_modules(tmp_path, monkeypatch)
    clone_parent = tmp_path / "clones"
    clone_parent.mkdir()
    cloned = clone_parent / "gitignore"
    cloned.mkdir()
    (cloned / "README.md").write_text("# gitignore\n", encoding="utf-8")
    (cloned / "Python.gitignore").write_text("__pycache__/\n", encoding="utf-8")

    monkeypatch.setattr(capsules, "clone_capsule_repository", lambda _url, _parent: cloned)

    missing = capsules.custom_tool_builder(
        "start",
        source_url="https://github.com/github/gitignore",
    )
    started = capsules.custom_tool_builder(
        "start",
        source_url="https://github.com/github/gitignore",
        fields={"clone_parent": str(clone_parent)},
    )

    assert missing["needs_input"] == "clone_parent"
    assert started["draft"]["installed_path"] == str(cloned.resolve())
    assert started["draft"]["source_url"] == "https://github.com/github/gitignore"
    assert started["draft"]["commands"]


def test_custom_tool_builder_recovers_clone_parent_passed_as_source_path(tmp_path, monkeypatch):
    capsules = _fresh_modules(tmp_path, monkeypatch)
    clone_parent = tmp_path / "custom-tool-test"
    clone_parent.mkdir()
    cloned = clone_parent / "gitignore"
    cloned.mkdir()
    (cloned / "README.md").write_text("# gitignore\n", encoding="utf-8")
    (cloned / "Python.gitignore").write_text("__pycache__/\n", encoding="utf-8")

    calls = {}

    def _fake_clone(url: str, parent: str):
        calls["url"] = url
        calls["parent"] = parent
        return cloned

    monkeypatch.setattr(capsules, "clone_capsule_repository", _fake_clone)

    started = capsules.custom_tool_builder(
        "start",
        source_path=str(clone_parent),
        source_url="https://github.com/github/gitignore",
    )

    assert calls == {"url": "https://github.com/github/gitignore", "parent": str(clone_parent)}
    assert started["draft"]["installed_path"] == str(cloned.resolve())
    assert started["draft"]["commands"]


def test_custom_tool_builder_missing_clone_parent_requires_create_approval(tmp_path, monkeypatch):
    capsules = _fresh_modules(tmp_path, monkeypatch)
    missing_parent = tmp_path / "missing-parent"
    cloned = missing_parent / "gitignore"

    def _fake_clone(_url: str, _parent: str):
        cloned.mkdir(parents=True, exist_ok=True)
        (cloned / "README.md").write_text("# gitignore\n", encoding="utf-8")
        return cloned

    monkeypatch.setattr(capsules, "clone_capsule_repository", _fake_clone)

    blocked = capsules.custom_tool_builder(
        "start",
        source_url="https://github.com/github/gitignore",
        fields={"clone_parent": str(missing_parent)},
    )
    started = capsules.custom_tool_builder(
        "start",
        source_url="https://github.com/github/gitignore",
        fields={"clone_parent": str(missing_parent), "create_clone_parent": True},
    )

    assert blocked["needs_input"] == "create_clone_parent"
    assert blocked["clone_parent"] == str(missing_parent.resolve())
    assert "does not exist" in blocked["message"]
    assert missing_parent.exists()
    assert started["draft"]["installed_path"] == str(cloned.resolve())


def test_custom_tool_builder_empty_draft_actions_are_friendly(tmp_path, monkeypatch):
    capsules = _fresh_modules(tmp_path, monkeypatch)

    for action in ("show", "refine", "update", "test", "create", "enable", "promote"):
        result = capsules.custom_tool_builder(action, draft_id="")
        assert result["blocked"] is True
        assert result["needs_input"] == "draft"
        assert "No Custom Tool draft is active yet" in result["message"]


def test_tool_capsule_clone_requires_explicit_parent(tmp_path, monkeypatch):
    capsules = _fresh_modules(tmp_path, monkeypatch)

    try:
        capsules.clone_capsule_repository("https://github.com/example/repo", str(tmp_path / "missing"))
    except ValueError as exc:
        assert "does not exist" in str(exc)
    else:
        raise AssertionError("clone should require an existing explicit parent folder")


def test_custom_tool_clone_reuses_existing_target_folder(tmp_path, monkeypatch):
    capsules = _fresh_modules(tmp_path, monkeypatch)
    target = tmp_path / "repo"
    target.mkdir()

    cloned = capsules.clone_capsule_repository("https://github.com/example/repo", str(tmp_path))

    assert cloned == target.resolve()


def test_tool_capsule_runner_uses_capsule_path_and_policy(tmp_path, monkeypatch):
    capsules = _fresh_modules(tmp_path, monkeypatch)
    install_path = tmp_path / "capsule"
    install_path.mkdir()

    capsule = capsules.register_capsule(
        "https://github.com/example/tool",
        name="Example Tool",
        installed_path=str(install_path),
    )
    capsules.set_capsule_enabled(capsule.id, True)

    result = capsules.run_capsule_command(capsule.id, "python --version", "read_only")

    assert result.ran is False
    assert result.decision.decision == "block"
    assert result.cwd == str(install_path.resolve())


def test_custom_tool_card_test_runner_requires_one_time_approval(tmp_path, monkeypatch):
    capsules = _fresh_modules(tmp_path, monkeypatch)
    install_path = tmp_path / "capsule"
    install_path.mkdir()

    capsule = capsules.register_capsule(
        "https://github.com/example/tool",
        name="Example Tool",
        installed_path=str(install_path),
    )
    command = "python -c \"import urllib.request; urllib.request.urlopen('https://example.com')\""

    pending = capsules.run_custom_tool_test_command(capsule.id, command, approved_once=False)

    assert pending.ran is False
    assert pending.decision.requires_approval is True
    assert "requires approval" in pending.stderr

    monkeypatch.setattr(capsules, "detect_container_runtime", lambda: SimpleNamespace(available=False))

    def fake_local(tool, cmd, decision):
        return capsules.CommandResult(
            command=cmd,
            cwd=tool.installed_path,
            returncode=0,
            stdout="approved once\n",
            decision=decision,
            execution_mode="local",
        )

    monkeypatch.setattr(capsules, "_run_custom_tool_local_direct", fake_local)
    approved = capsules.run_custom_tool_test_command(capsule.id, command, approved_once=True)

    assert approved.ok is True
    assert approved.stdout == "approved once\n"
    assert approved.decision.decision == "allow"
    assert approved.execution_mode == "local"


def test_custom_tool_test_runner_substitutes_query_placeholder(tmp_path, monkeypatch):
    capsules = _fresh_modules(tmp_path, monkeypatch)
    install_path = tmp_path / "capsule"
    install_path.mkdir()

    capsule = capsules.register_capsule(
        "https://github.com/example/tool",
        name="Example Tool",
        installed_path=str(install_path),
    )
    command = 'python -c "q = {query}.lower(); print(q)"'
    captured: dict[str, str] = {}

    def fake_run_workspace_command(cwd, cmd, approval_mode):
        captured["command"] = cmd
        return capsules.CommandResult(command=cmd, cwd=str(cwd), returncode=0, stdout="python\n")

    monkeypatch.setattr(capsules, "run_workspace_command", fake_run_workspace_command)

    result = capsules.run_custom_tool_test_command(capsule.id, command, require_enabled=False)

    assert result.ok is True
    assert "{query}" not in captured["command"]
    assert "'python'.lower()" in captured["command"]


def test_custom_tool_draft_test_substitutes_query_placeholder(tmp_path, monkeypatch):
    capsules = _fresh_modules(tmp_path, monkeypatch)
    install_path = tmp_path / "capsule"
    install_path.mkdir()
    draft = capsules.CustomToolDraft(
        id="draft-1",
        source_url="https://github.com/example/tool",
        installed_path=str(install_path),
        name="Example Tool",
        commands=[
            {
                "name": "Find",
                "command": 'python -c "import sys; print({query}.lower())"',
                "description": "Find things",
            }
        ],
    )
    capsules._save_draft(draft)
    captured: dict[str, str] = {}

    def fake_run_workspace_command(cwd, cmd, approval_mode):
        captured["command"] = cmd
        return capsules.CommandResult(command=cmd, cwd=str(cwd), returncode=0, stdout="python\n")

    monkeypatch.setattr(capsules, "run_workspace_command", fake_run_workspace_command)

    result = capsules.test_custom_tool_draft_command("draft-1")

    assert result.ok is True
    assert "{query}" not in captured["command"]
    assert "'python'.lower()" in captured["command"]


def test_tool_capsule_promotion_registers_plugin_tool_and_removes_safely(tmp_path, monkeypatch):
    capsules = _fresh_modules(tmp_path, monkeypatch)
    install_path = tmp_path / "capsule"
    install_path.mkdir()
    (install_path / "thoth-capsule.json").write_text(
        """
        {
          "name": "Repo Helper",
          "version": "1.0.0",
          "commands": [
            {"name": "Smoke", "command": "python --version", "description": "Check Python"}
          ]
        }
        """,
        encoding="utf-8",
    )

    capsule = capsules.register_capsule(
        "https://github.com/example/repo-helper",
        installed_path=str(install_path),
    )
    promoted = capsules.promote_capsule(capsule.id)

    from plugins import registry as plugin_registry
    from plugins import state as plugin_state

    manifest = plugin_registry.get_manifest(promoted.promoted_plugin_id)
    tool_names = {tool.name for tool in plugin_registry.get_langchain_tools()}

    assert manifest is not None
    assert "custom-tool" in manifest.tags
    assert plugin_state.is_plugin_enabled(promoted.promoted_plugin_id) is True
    assert promoted.enabled is True
    assert capsules.list_capsules()[0].enabled is True
    assert any(name.startswith("custom_tool_repo_helper_") for name in tool_names)

    capsules.remove_promoted_capsule_tool(capsule.id)

    assert install_path.exists()
    assert plugin_registry.get_manifest(promoted.promoted_plugin_id) is None
    assert capsules.list_capsules()[0].promoted_plugin_id == ""


def test_tool_capsule_developer_ui_exposes_end_to_end_actions():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    source = (root / "developer" / "ui.py").read_text(encoding="utf-8")
    capsules_source = (root / "developer" / "tool_capsules.py").read_text(encoding="utf-8")
    tool_source = (root / "tools" / "developer_tool.py").read_text(encoding="utf-8")
    global_tool_source = (root / "tools" / "custom_tool_builder_tool.py").read_text(encoding="utf-8")
    utilities_source = (root / "ui" / "settings.py").read_text(encoding="utf-8")
    guide_source = (root / "tool_guides" / "custom_tool_builder_guide" / "SKILL.md").read_text(encoding="utf-8")

    assert "Custom Tools" in source
    assert "New Custom Tool" in source
    assert "Repo URL or local folder" in source
    assert 'ui.stepper().props("vertical")' in source
    assert "Inspect Tool" in source
    assert "Create Tool" in source or "Create the tool" in source
    assert "create_custom_tool_draft" in source
    assert "create_tool_from_draft" in source
    assert "list_custom_tool_drafts" in source
    assert "clone_capsule_repository" in source
    assert "Run Smoke Test" in source
    assert "All commands" in source
    assert "run_custom_tool_test_command" in source
    assert "Test query" in source
    assert "custom_tool_command_needs_query" in source
    assert "Test First Command" not in source
    assert "Test" in source
    assert "promote_created_custom_tool_from_draft" in source
    assert "remove_capsule" in source
    assert "set_community_tools_enabled" not in source
    assert "generate_and_register_capsule" in capsules_source
    assert "custom_tool_builder" in capsules_source
    assert 'name="custom_tool_builder"' in global_tool_source
    assert "Use `custom_tool_builder` for lifecycle state" in guide_source
    assert "Shell can help with extra read-only inspection" in guide_source
    assert "Do not use shell to manually register" in guide_source
    assert '"custom_tool_builder"' in utilities_source
    assert "CUSTOM_TOOL_DRAFTS_PATH" in capsules_source
    assert 'name="developer_custom_tool_builder"' not in tool_source
    assert 'name="developer_inspect_custom_tool_source"' not in tool_source
    assert 'name="developer_create_custom_tool"' not in tool_source
    assert 'name="developer_test_custom_tool"' not in tool_source
    assert 'name="developer_enable_custom_tool"' not in tool_source
    assert 'name="developer_promote_custom_tool"' not in tool_source
    assert "Tool Capsule" not in source
    assert '("capsules", "Custom Tools"' not in source
