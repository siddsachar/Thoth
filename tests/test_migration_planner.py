"""Focused tests for Phase 3 migration preview planning."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path


class MigrationPlannerTests(unittest.TestCase):
    def test_builds_hermes_preview_plan_from_realistic_fixture(self) -> None:
        from migration import MigrationCategory, MigrationStatus, build_hermes_plan
        from migration.fixtures import create_realistic_hermes_home

        with tempfile.TemporaryDirectory() as temp_dir:
            source = create_realistic_hermes_home(Path(temp_dir) / ".hermes")
            plan = build_hermes_plan(source, target_root=Path(temp_dir) / "target")

        categories = {item.category for item in plan.items}
        self.assertTrue(plan.source.found)
        self.assertIn(MigrationCategory.MODEL, categories)
        self.assertIn(MigrationCategory.IDENTITY, categories)
        self.assertIn(MigrationCategory.MEMORIES, categories)
        self.assertIn(MigrationCategory.SKILLS, categories)
        self.assertIn(MigrationCategory.MCP, categories)
        self.assertIn(MigrationCategory.API_KEYS, categories)
        self.assertIn(MigrationCategory.ARCHIVE, categories)
        self.assertGreaterEqual(plan.summary.archive_only, 3)

        secret_items = [item for item in plan.items if item.category == MigrationCategory.API_KEYS]
        self.assertTrue(secret_items)
        self.assertTrue(all(item.status == MigrationStatus.SKIPPED for item in secret_items))
        self.assertTrue(all(not item.selected for item in secret_items))

        mcp_items = [item for item in plan.items if item.category == MigrationCategory.MCP]
        self.assertTrue(mcp_items)
        self.assertTrue(all(not item.selected for item in mcp_items))
        self.assertIn("Secrets were detected but skipped", "\n".join(plan.warnings))

        raw_text = json.dumps(plan.to_dict())
        self.assertNotIn("sk-hermes-three-month-user", raw_text)
        self.assertNotIn("linear-secret-token", raw_text)

    def test_include_secrets_marks_secret_items_sensitive(self) -> None:
        from migration import MigrationCategory, MigrationStatus, build_hermes_plan
        from migration.fixtures import create_realistic_hermes_home

        with tempfile.TemporaryDirectory() as temp_dir:
            source = create_realistic_hermes_home(Path(temp_dir) / ".hermes")
            plan = build_hermes_plan(source, target_root=Path(temp_dir) / "target", include_secrets=True)

        secret_items = [item for item in plan.items if item.category == MigrationCategory.API_KEYS]
        self.assertTrue(secret_items)
        self.assertTrue(all(item.status == MigrationStatus.SENSITIVE for item in secret_items))
        self.assertTrue(all(item.requires_confirmation for item in secret_items))
        self.assertTrue(any(item.id.startswith("api_keys:openai") for item in secret_items))
        self.assertTrue(any(item in plan.apply_candidates for item in secret_items))

    def test_builds_openclaw_preview_plan_from_legacy_fixture(self) -> None:
        from migration import MigrationCategory, MigrationStatus, build_openclaw_plan
        from migration.fixtures import create_realistic_openclaw_home

        with tempfile.TemporaryDirectory() as temp_dir:
            source = create_realistic_openclaw_home(Path(temp_dir) / ".clawdbot")
            plan = build_openclaw_plan(source, target_root=Path(temp_dir) / "target")

        categories = {item.category for item in plan.items}
        self.assertIn(MigrationCategory.MODEL, categories)
        self.assertIn(MigrationCategory.MCP, categories)
        self.assertIn(MigrationCategory.CHANNELS, categories)
        self.assertIn(MigrationCategory.SETTINGS, categories)
        self.assertIn(MigrationCategory.ARCHIVE, categories)
        self.assertTrue(any("legacy OpenClaw" in warning for warning in plan.warnings))
        self.assertTrue(any(item.id == "memories:daily-memory" for item in plan.items))
        daily_item = next(item for item in plan.items if item.id == "memories:daily-memory")
        self.assertTrue(str(daily_item.target).endswith("memory/daily-memory.md") or str(daily_item.target).endswith("memory\\daily-memory.md"))
        self.assertTrue(any(item.id == "mcp:filesystem" for item in plan.items))
        self.assertTrue(any(item.status == MigrationStatus.SKIPPED for item in plan.items if item.category == MigrationCategory.CHANNELS))
        self.assertGreaterEqual(plan.summary.archive_only, 4)

    def test_malformed_openclaw_config_warns_and_keeps_file_plan(self) -> None:
        from migration import MigrationCategory, build_openclaw_plan

        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / ".openclaw"
            _write(source / "openclaw.json", "{not json")
            _write(source / "workspace" / "MEMORY.md", "# Memory\n\nStill import this.\n")
            plan = build_openclaw_plan(source, target_root=Path(temp_dir) / "target")

        self.assertTrue(any("Could not parse OpenClaw" in warning for warning in plan.warnings))
        self.assertTrue(any(item.category == MigrationCategory.MEMORIES for item in plan.items))
        self.assertEqual(plan.summary.errors, 0)

    def test_target_conflicts_are_previewed_without_writes(self) -> None:
        from migration import MigrationStatus, build_hermes_plan
        from migration.fixtures import create_realistic_hermes_home

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            source = create_realistic_hermes_home(temp_root / ".hermes")
            target = temp_root / "target"
            _write(target / "identity" / "SOUL.md", "# Existing\n")
            before = _snapshot_tree(temp_root)
            plan = build_hermes_plan(source, target_root=target)
            after = _snapshot_tree(temp_root)

        self.assertEqual(before, after)
        self.assertTrue(any(item.status == MigrationStatus.CONFLICT for item in plan.items if item.id == "identity:soul.md"))
        self.assertTrue(plan.has_blocking_conflicts)

    def test_dispatcher_returns_empty_plan_for_missing_source(self) -> None:
        from migration import build_migration_plan

        with tempfile.TemporaryDirectory() as temp_dir:
            plan = build_migration_plan("hermes", Path(temp_dir) / "missing", target_root=Path(temp_dir) / "target")

        self.assertFalse(plan.source.found)
        self.assertEqual(plan.summary.total, 0)
        self.assertTrue(any("not found" in warning for warning in plan.warnings))

    def test_dispatcher_returns_empty_plan_for_wrong_provider_source(self) -> None:
        from migration import build_migration_plan
        from migration.fixtures import create_realistic_openclaw_home

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = create_realistic_openclaw_home(root / ".openclaw")
            plan = build_migration_plan("hermes", source, target_root=root / "target")

        self.assertFalse(plan.source.found)
        self.assertEqual(plan.summary.total, 0)
        self.assertTrue(any("Choose OpenClaw" in warning for warning in plan.warnings))


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _snapshot_tree(root: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            snapshot[str(path.relative_to(root)).replace("\\", "/")] = path.read_text(encoding="utf-8")
    return snapshot


if __name__ == "__main__":
    unittest.main(verbosity=2)
