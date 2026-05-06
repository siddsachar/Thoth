"""Focused tests for Phase 5 migration wizard helpers."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path


class MigrationWizardUiTests(unittest.TestCase):
    def test_settings_launches_migration_from_preferences(self) -> None:
        settings_source = Path("ui/settings.py").read_text(encoding="utf-8")

        self.assertIn("Open Migration Wizard", settings_source)
        self.assertIn("def _open_migration_wizard_dialog", settings_source)
        self.assertIn('"Migration": tab_prefs', settings_source)
        self.assertNotIn('ui.tab("Migration"', settings_source)
        self.assertGreater(settings_source.index("Open Migration Wizard"), settings_source.index("build_update_section"))

    def test_wizard_guidance_describes_safe_flow(self) -> None:
        from ui.migration_wizard import field_help_text, wizard_step_titles, workflow_steps

        steps = workflow_steps()
        titles = wizard_step_titles()
        self.assertGreaterEqual(len(steps), 4)
        self.assertEqual(titles, ("1. Choose folders", "2. Review scan", "3. Apply migration"))
        self.assertTrue(any("read-only" in step for step in steps))
        self.assertTrue(any("backups" in step for step in steps))
        self.assertIn("disposable", field_help_text("target"))
        self.assertIn("Off by default", field_help_text("secrets"))
        self.assertIn("backed up", field_help_text("overwrite"))

    def test_warnings_are_humanized_and_deduplicated(self) -> None:
        from ui.migration_wizard import friendly_warnings

        warnings = friendly_warnings((
            "secrets detected; import must be opt-in",
            "Secrets were detected but skipped. Re-run with include_secrets=True after explicit user consent.",
            "Archive-only state will be copied to a migration report later, not imported live.",
            "Conflicting target files were found. Resolve or choose an overwrite policy before apply.",
        ))

        joined = "\n".join(warnings)
        self.assertEqual(len(warnings), 3)
        self.assertIn("API keys or tokens were found", joined)
        self.assertIn("kept for reference only", joined)
        self.assertIn("target files already exist", joined)
        self.assertNotIn("include_secrets", joined)
        self.assertNotIn("opt-in", joined)

    def test_category_warnings_are_section_specific(self) -> None:
        from migration import MigrationCategory, MigrationStatus, build_hermes_plan
        from migration.fixtures import create_realistic_hermes_home
        from ui.migration_wizard import category_warning_texts

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = create_realistic_hermes_home(root / ".hermes")
            target = root / "target"
            _write(target / "identity" / "SOUL.md", "# Existing\n")
            plan = build_hermes_plan(source, target_root=target)

        grouped = plan.items_by_category()
        api_key_warnings = category_warning_texts(MigrationCategory.API_KEYS, grouped["api_keys"])
        archive_warnings = category_warning_texts(MigrationCategory.ARCHIVE, grouped["archive"])
        identity_warnings = category_warning_texts(MigrationCategory.IDENTITY, grouped["identity"])
        memory_warnings = category_warning_texts(MigrationCategory.MEMORIES, grouped["memories"])

        self.assertTrue(any("API keys or tokens" in warning for warning in api_key_warnings))
        self.assertTrue(any("reference only" in warning for warning in archive_warnings))
        self.assertTrue(any("already exist" in warning for warning in identity_warnings))
        self.assertEqual(memory_warnings, ())

        overwrite_identity = category_warning_texts(MigrationCategory.IDENTITY, grouped["identity"], overwrite=True)
        self.assertTrue(any("Overwrite is on" in warning for warning in overwrite_identity))

    def test_selection_helpers_preserve_plan_metadata(self) -> None:
        from migration import MigrationCategory, build_hermes_plan
        from migration.fixtures import create_realistic_hermes_home
        from ui.migration_wizard import category_counts, plan_with_selected_ids, selected_item_ids, selectable_item_ids

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = create_realistic_hermes_home(root / ".hermes")
            plan = build_hermes_plan(source, target_root=root / "target")

        original_selected = selected_item_ids(plan)
        selectable = selectable_item_ids(plan)
        self.assertTrue(original_selected)
        self.assertTrue(original_selected.issubset(selectable))

        skills = {item.id for item in plan.items if item.category == MigrationCategory.SKILLS}
        selected_plan = plan_with_selected_ids(plan, skills)
        self.assertEqual(selected_plan.source, plan.source)
        self.assertEqual(selected_plan.warnings, plan.warnings)
        self.assertEqual(selected_plan.metadata, plan.metadata)
        self.assertEqual(selected_item_ids(selected_plan), skills)

        rows = {row["category"]: row for row in category_counts(selected_plan)}
        self.assertGreaterEqual(rows["skills"]["selected"], 1)
        self.assertEqual(rows["api_keys"]["selected"], 0)

    def test_category_cards_and_sections_share_order(self) -> None:
        from migration import build_hermes_plan
        from migration.fixtures import create_realistic_hermes_home
        from ui.migration_wizard import category_counts, ordered_category_items

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = create_realistic_hermes_home(root / ".hermes")
            plan = build_hermes_plan(source, target_root=root / "target")

        card_order = [row["category"] for row in category_counts(plan)]
        section_order = [category for category, _items in ordered_category_items(plan)]

        self.assertEqual(card_order, section_order)
        self.assertEqual(card_order[:3], ["model", "settings", "mcp"])

    def test_conflicts_are_selectable_only_with_overwrite(self) -> None:
        from migration import MigrationStatus, build_hermes_plan
        from migration.fixtures import create_realistic_hermes_home
        from ui.migration_wizard import non_selectable_item_note, selectable_category_item_ids, selectable_item_ids

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = create_realistic_hermes_home(root / ".hermes")
            target = root / "target"
            _write(target / "identity" / "SOUL.md", "# Existing\n")
            plan = build_hermes_plan(source, target_root=target)

        conflict_ids = {item.id for item in plan.items if item.status == MigrationStatus.CONFLICT}
        self.assertTrue(conflict_ids)
        self.assertTrue(conflict_ids.isdisjoint(selectable_item_ids(plan, overwrite=False)))
        self.assertTrue(conflict_ids.issubset(selectable_item_ids(plan, overwrite=True)))
        self.assertEqual(selectable_category_item_ids(plan, "archive"), set())

        archive_item = next(item for item in plan.items if item.category.value == "archive")
        self.assertEqual(non_selectable_item_note(archive_item), "Report only")

        conflict_item = next(item for item in plan.items if item.status == MigrationStatus.CONFLICT)
        self.assertEqual(non_selectable_item_note(conflict_item), "Turn on overwrite to select")

    def test_apply_report_paths_are_exposed_without_secret_leak(self) -> None:
        from migration import apply_migration_plan, build_hermes_plan
        from migration.fixtures import create_realistic_hermes_home
        from ui.migration_wizard import result_report_paths

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = create_realistic_hermes_home(root / ".hermes")
            plan = build_hermes_plan(source, target_root=root / "target", include_secrets=True)
            result = apply_migration_plan(plan)
            paths = result_report_paths(result)
            result_text = (Path(paths["result"]).read_text(encoding="utf-8"))

        self.assertTrue(paths["report_dir"])
        self.assertTrue(paths["summary"].endswith("summary.md"))
        self.assertTrue(paths["backup_manifest"].endswith("backup_manifest.json"))
        self.assertNotIn("sk-hermes-three-month-user", result_text)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    unittest.main(verbosity=2)
