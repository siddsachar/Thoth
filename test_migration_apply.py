"""Focused tests for Phase 4 migration apply, backup, and reports."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path


class _FakeKeyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, account: str) -> str | None:
        return self.values.get((service, account))

    def set_password(self, service: str, account: str, value: str) -> None:
        self.values[(service, account)] = value

    def delete_password(self, service: str, account: str) -> None:
        self.values.pop((service, account), None)


def _install_fake_keyring():
    import secret_store
    backend = _FakeKeyring()
    secret_store._set_backend_for_tests(backend)
    return secret_store


class MigrationApplyTests(unittest.TestCase):
    def test_applies_selected_hermes_items_and_archives_report_only_state(self) -> None:
        from migration import MigrationStatus, apply_migration_plan, build_hermes_plan
        from migration.fixtures import create_realistic_hermes_home

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = create_realistic_hermes_home(root / ".hermes")
            target = root / "target"
            plan = build_hermes_plan(source, target_root=target)
            result = apply_migration_plan(plan)

            self.assertEqual(result.summary.errors, 0)
            self.assertTrue((target / "identity" / "SOUL.md").is_file())
            self.assertTrue((target / "memory" / "MEMORY.md").read_text(encoding="utf-8").find("Imported from") >= 0)
            self.assertTrue((target / "skills" / "ship-it" / "SKILL.md").is_file())
            self.assertTrue((target / "config" / "models.json").is_file())
            self.assertFalse((target / "api_keys.json").exists())
            self.assertTrue((result.report_dir / "archive" / "logs" / "hermes.log").is_file())
            self.assertTrue((result.report_dir / "plan.json").is_file())
            self.assertTrue((result.report_dir / "result.json").is_file())
            self.assertTrue(any(item.status == MigrationStatus.MIGRATED for item in result.items))

            report_text = (result.report_dir / "result.json").read_text(encoding="utf-8")
            archive_auth_text = (result.report_dir / "archive" / "auth.json").read_text(encoding="utf-8")
            archive_token_text = (result.report_dir / "archive" / "mcp-tokens" / "linear.json").read_text(encoding="utf-8")
            self.assertNotIn("sk-hermes-three-month-user", report_text)
            self.assertNotIn("linear-secret-token", report_text)
            self.assertNotIn("sk-legacy-auth", archive_auth_text)
            self.assertNotIn("linear-secret-token", archive_token_text)
            self.assertIn("[redacted]", archive_auth_text)
            self.assertIn("[redacted]", archive_token_text)

    def test_existing_targets_are_backed_up_before_overwrite(self) -> None:
        from migration import apply_migration_plan, build_hermes_plan
        from migration.fixtures import create_realistic_hermes_home

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = create_realistic_hermes_home(root / ".hermes")
            target = root / "target"
            plan = build_hermes_plan(source, target_root=target)
            _write(target / "identity" / "SOUL.md", "# Existing persona\n")
            result = apply_migration_plan(plan)

            self.assertTrue(result.backup_dir)
            backup = result.backup_dir / "identity" / "SOUL.md"
            self.assertTrue(backup.is_file())
            self.assertEqual(backup.read_text(encoding="utf-8"), "# Existing persona\n")
            self.assertNotEqual((target / "identity" / "SOUL.md").read_text(encoding="utf-8"), "# Existing persona\n")
            self.assertTrue(result.backup_manifest)

    def test_secret_apply_requires_explicit_secret_plan(self) -> None:
        from migration import apply_migration_plan, build_hermes_plan
        from migration.fixtures import create_realistic_hermes_home
        from api_keys import get_key_for_data_dir

        secret_store = _install_fake_keyring()

        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                source = create_realistic_hermes_home(root / ".hermes")
                target = root / "target"
                plan = build_hermes_plan(source, target_root=target, include_secrets=True)
                result = apply_migration_plan(plan)

                api_metadata = json.loads((target / "api_keys.json").read_text(encoding="utf-8"))
                self.assertEqual(get_key_for_data_dir(target, "OPENAI_API_KEY"), "sk-hermes-three-month-user")
                self.assertEqual(api_metadata["version"], 2)
                self.assertNotIn("sk-hermes-three-month-user", json.dumps(api_metadata))
                api_key_backups = [entry for entry in result.backup_manifest if entry["source"].endswith("api_keys.json")]
                self.assertEqual(api_key_backups, [])
                result_text = json.dumps(result.to_dict())
                self.assertNotIn("sk-hermes-three-month-user", result_text)
        finally:
            secret_store._set_backend_for_tests(None)

    def test_openclaw_daily_memory_import_uses_markdown_target(self) -> None:
        from migration import apply_migration_plan, build_openclaw_plan
        from migration.fixtures import create_realistic_openclaw_home

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = create_realistic_openclaw_home(root / ".openclaw")
            target = root / "target"
            plan = build_openclaw_plan(source, target_root=target)
            result = apply_migration_plan(plan)

            daily_memory = target / "memory" / "daily-memory.md"
            legacy_daily_path = target / "memory" / "daily"
            daily_memory_text = daily_memory.read_text(encoding="utf-8")

            self.assertEqual(result.summary.errors, 0)
            self.assertTrue(daily_memory.is_file())
            self.assertFalse(legacy_daily_path.exists())
            self.assertIn("2026-02.md", daily_memory_text)

    def test_multiple_secret_writes_backup_original_api_key_file_once(self) -> None:
        from migration import apply_migration_plan, build_hermes_plan
        from migration.fixtures import create_realistic_hermes_home
        from api_keys import get_key_for_data_dir

        secret_store = _install_fake_keyring()

        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                source = create_realistic_hermes_home(root / ".hermes")
                target = root / "target"
                original_api_keys = {"OPENAI_API_KEY": "sk-existing-thoth-fake"}
                _write(target / "api_keys.json", json.dumps(original_api_keys, indent=2) + "\n")
                plan = build_hermes_plan(source, target_root=target, include_secrets=True)
                result = apply_migration_plan(plan)

                api_metadata = json.loads((target / "api_keys.json").read_text(encoding="utf-8"))
                backup = json.loads((result.backup_dir / "api_keys.json").read_text(encoding="utf-8"))
                api_key_backups = [entry for entry in result.backup_manifest if entry["source"].endswith("api_keys.json")]

            self.assertEqual(backup, original_api_keys)
            self.assertEqual(len(api_key_backups), 1)
            self.assertEqual(api_metadata["version"], 2)
            self.assertNotIn("sk-hermes-three-month-user", json.dumps(api_metadata))
            self.assertEqual(get_key_for_data_dir(target, "ANTHROPIC_API_KEY"), "sk-ant-hermes-example")
            self.assertEqual(get_key_for_data_dir(target, "TELEGRAM_BOT_TOKEN"), "123456:hermes-demo-token")
            self.assertEqual(get_key_for_data_dir(target, "OPENAI_API_KEY"), "sk-hermes-three-month-user")
        finally:
            secret_store._set_backend_for_tests(None)

    def test_apply_requires_backup_unless_explicitly_overridden(self) -> None:
        from migration import MigrationApplyOptions, apply_migration_plan, build_hermes_plan
        from migration.fixtures import create_realistic_hermes_home

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = create_realistic_hermes_home(root / ".hermes")
            plan = build_hermes_plan(source, target_root=root / "target")
            with self.assertRaises(ValueError):
                apply_migration_plan(plan, MigrationApplyOptions(require_backup=False))
            result = apply_migration_plan(plan, MigrationApplyOptions(require_backup=False, allow_without_backup=True))

        self.assertIsNone(result.backup_dir)
        self.assertTrue(result.report_dir)

    def test_item_errors_do_not_stop_report_generation(self) -> None:
        from migration import (
            MigrationAction,
            MigrationCategory,
            MigrationItem,
            MigrationPlan,
            MigrationSource,
            MigrationStatus,
            apply_migration_plan,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = MigrationSource.from_path("hermes", root / ".hermes", found=True, confidence="high")
            good_source = root / "source.md"
            _write(good_source, "# Good\n")
            plan = MigrationPlan(
                source=source,
                items=(
                    MigrationItem(
                        id="identity:missing",
                        category=MigrationCategory.IDENTITY,
                        action=MigrationAction.COPY,
                        source=root / "missing.md",
                        target=root / "target" / "missing.md",
                    ),
                    MigrationItem(
                        id="identity:good",
                        category=MigrationCategory.IDENTITY,
                        action=MigrationAction.COPY,
                        source=good_source,
                        target=root / "target" / "good.md",
                    ),
                ),
                metadata={"target_root": str(root / "target")},
            )
            result = apply_migration_plan(plan)

            self.assertTrue((root / "target" / "good.md").is_file())
            self.assertTrue((result.report_dir / "result.json").is_file())
            self.assertTrue(any(item.status == MigrationStatus.ERROR for item in result.items))
            self.assertEqual(result.summary.errors, 1)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    unittest.main(verbosity=2)
