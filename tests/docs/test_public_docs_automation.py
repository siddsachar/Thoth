import json
from contextlib import ExitStack
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

import yaml
import pytest

from scripts.docs.collect_inventory import ROOT, build_inventory
from scripts.docs.generate_llms_txt import generate
from scripts.docs.generate_mdx import check_pages, render_pages
from scripts.docs.sync_github_pages import HAND_CURATED_MARKETING_FILES, check_sync, sync
from scripts.docs.validate_public_docs import validate


class _PublishedLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        if values.get("id"):
            self.ids.add(values["id"])
        if tag == "a" and values.get("href"):
            self.hrefs.append(values["href"])


def test_public_docs_inventory_has_core_sections() -> None:
    inventory = build_inventory()

    assert inventory["version"]["version"] != "unknown"
    assert any(tool["id"] == "browser" for tool in inventory["tools"])
    assert any(provider["id"] == "ollama" for provider in inventory["providers"])
    assert any(tab["title"] == "Providers" for tab in inventory["settings"])
    assert any(tab["title"] == "Workflows" for tab in inventory["home_tabs"])
    assert any(channel["id"] == "telegram" for channel in inventory["channels"])
    assert any(skill["id"] == "task_automation" for skill in inventory["skills"])
    assert any(server["id"] == "microsoft-playwright" for server in inventory["mcp"])
    assert any(plugin["id"] == "plugin-manifest" for plugin in inventory["plugins"])
    assert any(path["id"] == "threads_db" for path in inventory["data_paths"])
    assert any(rule["id"] == "approve" for rule in inventory["safety"])
    assert any(page["path"] == "index.mdx" for page in inventory["docs_pages"])
    assert {item["tab"] for item in inventory["settings_controls"]} == {
        "Accounts", "Buddy", "Channels", "Documents", "Knowledge", "MCP",
        "Models", "Plugins", "Preferences", "Providers", "Skills", "Tools",
        "System", "Tracker", "Utilities", "Voice",
    }
    assert inventory["cli_options"]
    assert inventory["environment"]


def test_progressive_tools_and_skills_are_documented_at_public_entry_points() -> None:
    guide = (ROOT / "docs-site" / "docs" / "guides" / "progressive-tools-and-skills.mdx").read_text(
        encoding="utf-8"
    )
    tools = (ROOT / "docs-site" / "docs" / "settings" / "tools.mdx").read_text(
        encoding="utf-8"
    )
    skills = (ROOT / "docs-site" / "docs" / "skills" / "index.mdx").read_text(
        encoding="utf-8"
    )
    docs_index = (ROOT / "docs-site" / "docs" / "index.mdx").read_text(encoding="utf-8")
    marketing = (ROOT / "docs" / "features.html").read_text(encoding="utf-8")
    generated_controls = (
        ROOT / "docs-site" / "docs" / "reference" / "generated" / "settings-controls.mdx"
    ).read_text(encoding="utf-8")

    for phrase in (
        "Auto-select external tools",
        "Load all external tools",
        "MCP servers, plugins, Custom Tools, and channels",
        "Up to five automatically selected skills",
        "cannot grant a tool that its profile denies",
        "real integration name",
    ):
        assert phrase in guide
    assert "progressive capability loading" in tools
    assert "parent task or child Agent" in skills
    assert "/docs/guides/progressive-tools-and-skills" in docs_index
    assert "Progressive external tools" in marketing
    assert "Auto-select external tools (recommended)" in generated_controls
    assert "Load all external tools" in generated_controls


def test_reasoning_controls_are_documented_at_public_entry_points() -> None:
    docs_root = ROOT / "docs-site" / "docs"
    guide = (docs_root / "chat" / "reasoning-controls.mdx").read_text(encoding="utf-8")
    chat = (docs_root / "chat" / "index.mdx").read_text(encoding="utf-8")
    models = (docs_root / "configuration" / "models-and-providers.mdx").read_text(
        encoding="utf-8"
    )
    channels = (docs_root / "integrations" / "channels.mdx").read_text(encoding="utf-8")
    sidebar = (ROOT / "docs-site" / "sidebars.ts").read_text(encoding="utf-8")

    for phrase in (
        "Provider default",
        "one thread and one exact provider-qualified model",
        "/reasoning budget 4096",
        "retries once with Provider default",
        "Settings -> Providers",
    ):
        assert phrase in guide
    assert "/docs/chat/reasoning-controls" in chat
    assert "/docs/chat/reasoning-controls" in models
    assert "`/reasoning`" in channels
    assert "'chat/reasoning-controls'" in sidebar


def test_all_published_html_internal_links_resolve() -> None:
    publish_root = ROOT / "docs"
    pages = sorted(publish_root.rglob("*.html"))
    parsed: dict[Path, _PublishedLinkParser] = {}

    def parse(path: Path) -> _PublishedLinkParser:
        if path not in parsed:
            parser = _PublishedLinkParser()
            parser.feed(path.read_text(encoding="utf-8"))
            parsed[path] = parser
        return parsed[path]

    for page in pages:
        for href in parse(page).hrefs:
            parts = urlsplit(href)
            if parts.scheme or parts.netloc or href.startswith(("mailto:", "tel:")):
                continue
            if parts.path.startswith("/"):
                target = publish_root / unquote(parts.path.lstrip("/"))
            else:
                target = page.parent / unquote(parts.path)
            if not parts.path:
                target = page
            if target.is_dir() or (not target.suffix and (target / "index.html").is_file()):
                target = target / "index.html"
            assert target.is_file(), f"{page.relative_to(publish_root)}: {href}"
            if parts.fragment:
                assert unquote(parts.fragment) in parse(target).ids, (
                    f"{page.relative_to(publish_root)}: {href}"
                )


def test_generated_mdx_pages_are_current() -> None:
    errors = check_pages(render_pages(build_inventory()))
    assert errors == []


def test_generated_mdx_is_stable_after_inventory_json_round_trip() -> None:
    inventory = build_inventory()
    serialized = json.loads(json.dumps(inventory, sort_keys=True))

    assert render_pages(inventory) == render_pages(serialized)


def test_github_pages_sync_preserves_marketing_files(tmp_path: Path) -> None:
    build_dir = tmp_path / "build"
    publish_dir = tmp_path / "publish"
    for name in ("assets", "docs", "img", "pagefind", "search"):
        source = build_dir / name
        source.mkdir(parents=True)
        (source / "artifact.txt").write_text(name, encoding="utf-8")
    pagefind = build_dir / "pagefind"
    (pagefind / "fragment").mkdir()
    (pagefind / "index").mkdir()
    (pagefind / "pagefind.js").write_text("runtime", encoding="utf-8")
    (pagefind / "pagefind-ui.css").write_text("styles", encoding="utf-8")
    (pagefind / "wasm.unknown.pagefind").write_bytes(b"wasm")
    (pagefind / "fragment" / "source.pf_fragment").write_bytes(b"fragment")
    (pagefind / "index" / "source.pf_index").write_bytes(b"index")
    (pagefind / "pagefind.en_source.pf_meta").write_bytes(b"metadata")
    (pagefind / "pagefind-entry.json").write_text(
        json.dumps({"version": "1", "languages": {"en": {"hash": "source", "page_count": 1}}}),
        encoding="utf-8",
    )
    for name in ("llms-full.txt", "llms.txt", "sitemap.xml"):
        (build_dir / name).write_text(name, encoding="utf-8")
    publish_dir.mkdir()
    marketing_files = {}
    for index, name in enumerate(HAND_CURATED_MARKETING_FILES):
        marketing = publish_dir / name
        payload = f"marketing-{index}".encode()
        marketing.write_bytes(payload)
        marketing_files[marketing] = payload
    obsolete = publish_dir / "docs.html"
    obsolete.write_text("old route format", encoding="utf-8")

    sync(build_dir, publish_dir)

    assert check_sync(build_dir, publish_dir) == []
    assert all(path.read_bytes() == payload for path, payload in marketing_files.items())
    assert not obsolete.exists()
    published_entry = publish_dir / "pagefind" / "pagefind-entry.json"
    published_entry.write_text(
        json.dumps({"version": "1", "languages": {"en": {"hash": "linux", "page_count": 1}}}),
        encoding="utf-8",
    )
    assert check_sync(build_dir, publish_dir) == []
    (publish_dir / "docs" / "artifact.txt").write_text("stale", encoding="utf-8")
    assert check_sync(build_dir, publish_dir) == [
        f"Published directory is stale: {(publish_dir / 'docs').resolve()}"
    ]


def test_llms_txt_generation_covers_docs_routes(tmp_path: Path) -> None:
    docs_root = ROOT / "docs-site" / "docs"
    generate(docs_root, tmp_path)

    llms = (tmp_path / "llms.txt").read_text(encoding="utf-8")
    llms_full = (tmp_path / "llms-full.txt").read_text(encoding="utf-8")

    assert "# Row-Bot Docs" in llms
    assert "[Row-Bot Documentation](/docs/)" in llms
    assert "Route: /docs/reference/generated/tools" in llms
    assert "Route: /docs/" in llms_full
    assert (tmp_path / "docs" / "llms.txt").is_file()
    assert (tmp_path / "docs" / "llms-full.txt").is_file()
    for path in sorted(docs_root.rglob("*.mdx")) + sorted(docs_root.rglob("*.md")):
        from scripts.docs.schemas import public_route_for_doc

        assert public_route_for_doc(path, docs_root) in llms


def test_docs_capture_is_opt_in_and_seed_data_is_safe(tmp_path: Path, monkeypatch) -> None:
    from row_bot.docs_capture import (
        is_docs_capture,
        is_docs_real_data_capture,
        load_docs_capture_demo_state,
        scan_demo_data_safety,
        write_docs_capture_demo_state,
    )

    monkeypatch.delenv("ROW_BOT_DOCS_CAPTURE", raising=False)
    monkeypatch.delenv("ROW_BOT_DOCS_REAL_DATA", raising=False)
    assert not is_docs_capture()
    assert not is_docs_real_data_capture()

    monkeypatch.setenv("ROW_BOT_DOCS_CAPTURE", "1")
    assert is_docs_capture()
    assert not is_docs_real_data_capture()
    monkeypatch.setenv("ROW_BOT_DOCS_REAL_DATA", "1")
    assert is_docs_real_data_capture()
    write_docs_capture_demo_state(tmp_path, scenario="full")
    data = load_docs_capture_demo_state(tmp_path)
    payload = json.dumps(data, sort_keys=True)

    assert "example.com" in payload
    assert "sk-" not in payload
    assert "ghp_" not in payload
    assert "C:\\Users\\" not in payload
    assert "/Users/" not in payload
    assert scan_demo_data_safety(tmp_path) == []


def test_screenshot_manifest_is_real_ui_and_safe() -> None:
    manifest_path = ROOT / "docs-content" / "metadata" / "screenshots.yml"
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    screenshots = data["screenshots"]

    required = [shot for shot in screenshots.values() if shot["status"] == "required"]
    assert len(screenshots) >= 20
    assert len(required) >= 20
    assert all(shot["status"] in {"required", "deferred"} for shot in screenshots.values())
    assert all(shot.get("alt") for shot in screenshots.values())
    assert all(not shot.get("route", "").startswith("/docs-mode/surface/") for shot in screenshots.values())
    assert all("/docs-mode/" not in shot.get("route", "") for shot in screenshots.values())
    assert all(shot.get("route", "/").startswith("/") for shot in required)
    assert all(shot.get("capture_selector") for shot in required)
    assert all(shot.get("expected_text") for shot in required)
    assert all(shot.get("source") in {"isolated-demo-data", "isolated-first-launch"} for shot in required)
    expected_dimensions = {"desktop": (3840, 2160), "wide": (3840, 2160), "mobile": (390, 844)}
    assert all(shot.get("viewport") in expected_dimensions for shot in required)
    assert screenshots["skills-hub"]["route"] == "/?dialog=skills-hub"
    assert screenshots["mcp-marketplace"]["route"] == "/?dialog=mcp-marketplace"


def test_mobile_screenshots_render_at_native_width() -> None:
    component = (ROOT / "docs-site" / "src" / "components" / "Screenshot.tsx").read_text(encoding="utf-8")
    styles = (ROOT / "docs-site" / "src" / "css" / "custom.css").read_text(encoding="utf-8")

    assert "id.startsWith('mobile-')" in component
    assert "const SCREENSHOT_REVISION = '4.9.0';" in component
    assert ".png?v=${SCREENSHOT_REVISION}" in component
    assert "rowBotScreenshotMobile" in component
    assert "width={isMobile ? 390 : undefined}" in component
    assert ".rowBotScreenshotMobile" in styles
    assert "width: min(100%, 390px);" in styles


def test_conceptual_guides_link_to_configuration_pages() -> None:
    concepts = ROOT / "docs-site" / "docs" / "concepts"
    expected = {
        "request-lifecycle.mdx": "/docs/configuration/models-and-providers",
        "memory-knowledge-and-dream-cycle.mdx": "/docs/settings/knowledge",
        "profiles-goals-and-agents.mdx": "/docs/profiles-goals-agents/",
        "background-workflows.mdx": "/docs/guides/workflows",
        "extensions-and-trust.mdx": "/docs/extending/",
    }

    for filename, configuration_route in expected.items():
        content = (concepts / filename).read_text(encoding="utf-8")
        assert configuration_route in content


def test_docs_navigation_returns_to_the_marketing_landing_page() -> None:
    config = (ROOT / "docs-site" / "docusaurus.config.ts").read_text(encoding="utf-8")
    docs_home = (ROOT / "docs-site" / "src" / "pages" / "index.tsx").read_text(
        encoding="utf-8"
    )

    assert "const landingPageUrl = 'https://row-bot.ai/';" in config
    assert config.count("href: landingPageUrl") == 3
    assert "{href: landingPageUrl, label: 'Home'" in config
    assert "label: 'Download'" in config
    assert "href: installationUrl" in config
    assert "const installationUrl = 'https://row-bot.ai/#install';" in config
    assert "https://row-bot.ai/features.html" in config
    assert "https://row-bot.ai/architecture.html" in config
    assert "https://row-bot.ai/contact.html" in config
    assert "github.com/siddsachar/row-bot/releases/latest" not in config
    assert 'href="https://row-bot.ai/#install"' in docs_home
    assert "github.com/siddsachar/row-bot/releases/latest" not in docs_home


def test_sitemap_source_includes_marketing_pages_without_shadow_copies() -> None:
    config = (ROOT / "docs-site" / "docusaurus.config.ts").read_text(encoding="utf-8")

    for url in (
        "https://row-bot.ai/features.html",
        "https://row-bot.ai/architecture.html",
        "https://row-bot.ai/contact.html",
    ):
        assert url in config
    assert "404.html" not in config
    assert not (ROOT / "docs-site" / "static" / "architecture.html").exists()
    assert not (ROOT / "docs-site" / "static" / "contact.html").exists()


def test_docs_ci_build_regenerates_llm_exports_before_building() -> None:
    package = json.loads(
        (ROOT / "docs-site" / "package.json").read_text(encoding="utf-8")
    )
    scripts = package["scripts"]

    assert scripts["generate:llms"] == (
        "python ../scripts/docs/generate_llms_txt.py "
        "--docs-root docs-site/docs --out-dir docs-site/static"
    )
    assert scripts["build:ci"].startswith("npm run generate:llms && ")


def test_docs_workflow_uses_the_canonical_build_container() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "docs.yml").read_text(encoding="utf-8")
    )
    build_job = workflow["jobs"]["build"]
    image = build_job["env"]["DOCS_NODE_IMAGE"]
    platform = build_job["env"]["DOCS_NODE_PLATFORM"]
    steps = build_job["steps"]
    build_step = next(
        step
        for step in steps
        if step["name"] == "Build and verify published docs in canonical container"
    )

    assert image == "node:20.20.2-bookworm"
    assert platform == "linux/amd64"
    assert not any(
        step.get("uses", "").startswith("actions/setup-node") for step in steps
    )
    assert "docker run --rm" in build_step["run"]
    assert '"$DOCS_NODE_PLATFORM"' in build_step["run"]
    assert '"$DOCS_NODE_IMAGE"' in build_step["run"]
    assert "npm ci && npm run build:ci" in build_step["run"]
    assert "sync_github_pages.py --check" in build_step["run"]
    readme = (ROOT / "docs-site" / "README.md").read_text(encoding="utf-8")
    assert image in readme
    assert f"--platform {platform}" in readme


def test_docs_build_inputs_have_canonical_line_endings() -> None:
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")

    for pattern in (
        "docs-content/**/*.md",
        "docs-content/**/*.yml",
        "docs-site/**/*.css",
        "docs-site/**/*.js",
        "docs-site/**/*.json",
        "docs-site/**/*.md",
        "docs-site/**/*.mdx",
        "docs-site/**/*.ts",
        "docs-site/**/*.tsx",
        "docs/assets/**/*.js",
        "docs/docs/**/*.html",
        "docs/pagefind/**/*.js",
        "docs/search/**/*.html",
        "scripts/docs/**/*.py",
    ):
        assert f"{pattern} text eol=lf" in attributes


def test_authoritative_surface_map_has_one_outcome_per_surface() -> None:
    data = yaml.safe_load(
        (ROOT / "docs-content" / "metadata" / "ui_surfaces.yml").read_text(encoding="utf-8")
    )
    surfaces = data["surfaces"]
    screenshots = yaml.safe_load(
        (ROOT / "docs-content" / "metadata" / "screenshots.yml").read_text(encoding="utf-8")
    )["screenshots"]

    assert data["authority"] == "public-docs-surface-coverage"
    assert len(surfaces) >= 40
    for surface in surfaces.values():
        assert surface["status"] in {"ready", "missing"}
        assert surface["capture_type"] in {"automated", "manual"}
        assert bool(surface.get("screenshot_id")) != bool(surface.get("no_image_reason"))
        if surface.get("screenshot_id"):
            assert surface["screenshot_id"] in screenshots


def test_capture_rejects_the_real_user_data_directory(tmp_path: Path, monkeypatch) -> None:
    import scripts.docs.capture_real_ui_screenshots as capture

    real_dir = tmp_path / "real-profile"
    real_dir.mkdir()
    monkeypatch.setattr(capture, "_real_user_data_dir", lambda: real_dir.resolve())

    try:
        capture._safe_capture_data_dir(real_dir)
    except RuntimeError as exc:
        assert "normal Row-Bot data directory" in str(exc)
    else:  # pragma: no cover - explicit safety failure
        raise AssertionError("capture accepted the real Row-Bot data directory")


def test_capture_accepts_real_data_only_with_explicit_reviewed_opt_in(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import scripts.docs.capture_real_ui_screenshots as capture

    real_dir = tmp_path / "real-profile"
    other_dir = tmp_path / "other-profile"
    real_dir.mkdir()
    other_dir.mkdir()
    monkeypatch.setattr(capture, "_real_user_data_dir", lambda: real_dir.resolve())

    selected, temporary = capture._safe_capture_data_dir(
        real_dir,
        authorize_real_data=True,
    )

    assert selected == real_dir.resolve()
    assert temporary is None
    with pytest.raises(RuntimeError, match="exact normal Row-Bot data directory"):
        capture._safe_capture_data_dir(other_dir, authorize_real_data=True)


def test_authorized_real_capture_cannot_seed_or_switch_to_temporary_data(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import scripts.docs.capture_real_ui_screenshots as capture

    real_dir = tmp_path / "real-profile"
    real_dir.mkdir()
    monkeypatch.setattr(capture, "_real_user_data_dir", lambda: real_dir.resolve())

    with pytest.raises(RuntimeError, match="--no-seed-demo-data"):
        capture.capture(
            {},
            scenario="full",
            data_dir=real_dir,
            seed_demo_data=True,
            use_temp_data=False,
            authorize_real_data=True,
        )
    with pytest.raises(RuntimeError, match="retained data"):
        capture.capture(
            {},
            scenario="full",
            data_dir=real_dir,
            seed_demo_data=False,
            use_temp_data=True,
            authorize_real_data=True,
        )


def test_authorized_real_capture_does_not_offer_fake_provider_choices(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import scripts.docs.capture_real_ui_screenshots as capture

    launched: dict[str, object] = {}

    def fake_popen(*args, **kwargs):
        launched["args"] = args
        launched["kwargs"] = kwargs
        return SimpleNamespace()

    monkeypatch.setattr(capture, "LOG_ROOT", tmp_path / "logs")
    monkeypatch.setattr(capture.subprocess, "Popen", fake_popen)
    with ExitStack() as stack:
        capture._launch_app(43123, tmp_path / "profile", stack, real_data=True)

    env = launched["kwargs"]["env"]
    assert env["ROW_BOT_DOCS_REAL_DATA"] == "1"
    assert env["ROW_BOT_DOCS_FAKE_PROVIDERS"] == "0"


def test_authorized_real_capture_keeps_model_defaults_read_only(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import row_bot.models as models
    import row_bot.vision as vision

    model_path = tmp_path / "model_settings.json"
    vision_path = tmp_path / "vision_settings.json"
    model_original = b'{"model":"model:codex:gpt-5.6-sol"}'
    vision_original = b'{"model":"model:codex:gpt-5.6-sol"}'
    model_path.write_bytes(model_original)
    vision_path.write_bytes(vision_original)
    monkeypatch.setattr(models, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(models, "_SETTINGS_PATH", model_path)
    monkeypatch.setattr(vision, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(vision, "_SETTINGS_PATH", vision_path)
    monkeypatch.setenv("ROW_BOT_DOCS_CAPTURE", "1")
    monkeypatch.setenv("ROW_BOT_DOCS_REAL_DATA", "1")

    models._save_settings({"model": "model:ollama:llama3.1:8b"})
    vision._save_settings({"model": "model:ollama:llama3.1:8b"})

    assert model_path.read_bytes() == model_original
    assert vision_path.read_bytes() == vision_original


def test_authorized_real_capture_uses_stable_anchors_not_demo_text() -> None:
    import scripts.docs.capture_real_ui_screenshots as capture

    selected = capture._real_data_shot({
        "wait_for": '[data-docs-id="home-panel-workflows"]',
        "expected_text": ["Morning Brief"],
        "actions": [
            {"click_selector": '[data-docs-id="profile-library-toggle"]'},
            {"wait_for_text": "Research Guide"},
            {"click_text": "filesystem.search"},
        ],
    })

    assert selected["wait_for"] == '[data-docs-id="home-panel-workflows"]'
    assert selected["expected_text"] == []
    assert selected["actions"] == [
        {"click_selector": '[data-docs-id="profile-library-toggle"]'},
        {"click_selector": '[data-docs-id="tool-trace"]'},
    ]


def test_capture_publication_atomically_replaces_an_existing_asset(
    tmp_path: Path,
) -> None:
    import scripts.docs.capture_real_ui_screenshots as capture

    source = tmp_path / "rendered.png"
    destination = tmp_path / "public" / "surface.png"
    source.write_bytes(b"new image")
    destination.parent.mkdir()
    destination.write_bytes(b"old image")

    capture._publish_capture(source, destination)

    assert destination.read_bytes() == b"new image"
    assert list(destination.parent.glob(".*.tmp")) == []


def test_authorized_real_capture_is_review_only_not_a_public_asset() -> None:
    source = (
        ROOT
        / "scripts"
        / "docs"
        / "capture_real_ui_screenshots.py"
    ).read_text(encoding="utf-8")

    assert "if not real_data:" in source
    assert "_publish_capture(raw_output, output)" in source
    assert "raw_output if real_data else output" in source


def test_authorized_real_mobile_detail_selects_a_real_chat_thread(
    monkeypatch,
) -> None:
    import row_bot.docs_capture as capture

    monkeypatch.setenv("ROW_BOT_DOCS_CAPTURE", "1")
    monkeypatch.setenv("ROW_BOT_DOCS_REAL_DATA", "1")
    monkeypatch.setattr(
        capture,
        "_list_real_capture_threads",
        lambda: [("real-chat", "Private name", "", "", "", "", "chat")],
    )
    state = SimpleNamespace(
        active_designer_project=None,
        active_developer_workspace_id=None,
        mobile_view="",
        mobile_chat_mode="threads",
        thread_id=None,
        thread_name=None,
        thread_model_override="",
        messages=[],
    )

    capture.configure_docs_capture_state(
        state,
        {
            "mobile_view": "chat",
            "thread_id": capture.DEMO_THREAD_ID,
        },
        load_messages=lambda thread_id: [{"role": "user", "content": thread_id}],
    )

    assert state.thread_id == "real-chat"
    assert state.mobile_chat_mode == "thread"
    assert state.messages == [{"role": "user", "content": "real-chat"}]


def test_buddy_overlay_public_docs_cover_the_complete_user_workflow() -> None:
    buddy = (ROOT / "docs-site" / "docs" / "settings" / "buddy.mdx").read_text(
        encoding="utf-8"
    ).casefold()
    voice_and_buddy = (
        ROOT / "docs-site" / "docs" / "voice-and-buddy" / "index.mdx"
    ).read_text(encoding="utf-8").casefold()
    chat = (ROOT / "docs-site" / "docs" / "chat" / "index.mdx").read_text(
        encoding="utf-8"
    ).casefold()
    readme = (ROOT / "README.md").read_text(encoding="utf-8").casefold()
    writer = (
        ROOT / "scripts" / "docs" / "write_public_user_guide_pages.py"
    ).read_text(encoding="utf-8").casefold()
    combined = "\n".join((buddy, voice_and_buddy))

    for phrase in (
        "always-on-top",
        "open full thread",
        "dock buddy",
        "hide buddy",
        "enter sends",
        "shift+enter",
        "saved draft",
        "simple approvals",
        "complex approvals",
        "stop",
        "windows and macos",
        "browser/server mode",
        "talk and dictate",
    ):
        assert phrase in combined
    assert "/docs/settings/buddy" in chat
    assert "buddy desktop overlay" in readme
    for phrase in (
        "drag buddy itself",
        "simple approvals",
        "complex approvals",
        "talk and dictate remain",
        "/docs/settings/buddy",
    ):
        assert phrase in writer
    for obsolete in (
        "enable switches decide whether buddy appears",
        "open and close overlay buttons",
        "toggle buddy visibility or reopen the overlay",
    ):
        assert obsolete not in writer


def test_docs_capture_never_reads_the_keyring(monkeypatch) -> None:
    import row_bot.secret_store as secret_store

    class FailingBackend:
        def get_password(self, *_args):
            raise AssertionError("keyring backend was read")

    monkeypatch.setenv("ROW_BOT_DOCS_CAPTURE", "1")
    monkeypatch.setattr(secret_store, "_backend_override", FailingBackend())

    assert secret_store.is_available() is False
    assert secret_store.get_secret("OPENAI_API_KEY") is None


def test_real_home_and_settings_tabs_have_routes() -> None:
    settings = yaml.safe_load((ROOT / "docs-content" / "metadata" / "settings.yml").read_text(encoding="utf-8"))["tabs"]
    home = yaml.safe_load((ROOT / "docs-content" / "metadata" / "home_tabs.yml").read_text(encoding="utf-8"))["tabs"]
    expected_settings = {
        "Providers",
        "Models",
        "Documents",
        "Tools",
        "Skills",
        "System",
        "Accounts",
        "Utilities",
        "Tracker",
        "Knowledge",
        "Buddy",
        "Voice",
        "Channels",
        "MCP",
        "Plugins",
        "Preferences",
    }
    expected_home = {"Workflows", "Designer", "Developer", "Knowledge", "Monitor"}
    assert set(settings) == expected_settings
    assert set(home) == expected_home
    assert all(str(meta.get("docs_route", "")).startswith("/docs/") for meta in settings.values())
    assert all(str(meta.get("docs_route", "")).startswith("/docs/") for meta in home.values())


def test_validator_rejects_fake_docs_screenshot_route(monkeypatch) -> None:
    import scripts.docs.validate_public_docs as validator

    original = validator._load_yaml

    def fake_load(path: Path):
        data = original(path)
        if path.name == "screenshots.yml":
            data = json.loads(json.dumps(data))
            first = next(iter(data["screenshots"].values()))
            first["route"] = "/docs-mode/surface/fake"
        return data

    monkeypatch.setattr(validator, "_load_yaml", fake_load)
    assert any("forbidden fake docs route" in error for error in validator.validate())


def test_public_docs_metadata_validates() -> None:
    assert validate() == []
