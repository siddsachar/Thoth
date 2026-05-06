"""Focused tests for the migration wizard Phase 1 foundation."""

from __future__ import annotations

import json
import unittest
from pathlib import Path


class MigrationCoreTests(unittest.TestCase):
    def test_provider_normalization_and_source_serialization(self) -> None:
        from migration import MigrationProvider, MigrationSource, normalize_provider

        self.assertEqual(normalize_provider("hermes-agent"), MigrationProvider.HERMES)
        self.assertEqual(normalize_provider("ClawdBot"), MigrationProvider.OPENCLAW)
        self.assertEqual(normalize_provider("elsewhere"), MigrationProvider.UNKNOWN)

        source = MigrationSource.from_path(
            "openclaw",
            "~/fake-openclaw",
            confidence="high",
            found=True,
            discovered_files=["openclaw.json", "workspace/MEMORY.md"],
            warnings=["legacy config name detected"],
        )
        payload = source.to_dict()
        self.assertEqual(payload["provider"], "openclaw")
        self.assertTrue(payload["root"].endswith("fake-openclaw"))
        self.assertEqual(payload["discovered_files"], ["openclaw.json", "workspace/MEMORY.md"])

    def test_item_id_is_stable_and_report_safe(self) -> None:
        from migration import MigrationCategory, make_item_id

        self.assertEqual(make_item_id(MigrationCategory.SKILLS, "Ship It!"), "skills:ship-it")
        self.assertEqual(make_item_id("api_keys", "OPENAI API Key"), "api_keys:openai-api-key")
        self.assertEqual(make_item_id("mcp", ""), "mcp:item")

    def test_secret_items_are_not_selected_by_default(self) -> None:
        from migration import (
            MigrationAction,
            MigrationCategory,
            MigrationItem,
            MigrationSensitivity,
            MigrationStatus,
        )

        item = MigrationItem(
            id="api_keys:openai",
            category=MigrationCategory.API_KEYS,
            action=MigrationAction.UPDATE,
            details={"env_var": "OPENAI_API_KEY", "value": "sk-test-secret"},
            sensitivity=MigrationSensitivity.SECRET,
        )
        self.assertEqual(item.status, MigrationStatus.SENSITIVE)
        self.assertFalse(item.selected)
        self.assertTrue(item.requires_confirmation)
        self.assertFalse(item.is_apply_candidate)

    def test_archive_only_items_are_never_apply_candidates(self) -> None:
        from migration import MigrationAction, MigrationCategory, MigrationItem, MigrationStatus

        item = MigrationItem(
            id="archive:sessions",
            category=MigrationCategory.ARCHIVE,
            action=MigrationAction.ARCHIVE,
            status=MigrationStatus.ARCHIVE_ONLY,
            source=Path("sessions"),
            target=Path("migration/archive/sessions"),
            selected=True,
        )
        self.assertFalse(item.selected)
        self.assertTrue(item.is_archive_only)
        self.assertFalse(item.is_apply_candidate)

    def test_plan_summary_and_blocking_conflict_detection(self) -> None:
        from migration import (
            ConflictPolicy,
            MigrationAction,
            MigrationCategory,
            MigrationItem,
            MigrationPlan,
            MigrationSource,
            MigrationStatus,
        )

        source = MigrationSource.from_path("hermes", "C:/Users/test/.hermes", found=True, confidence="high")
        ready = MigrationItem("skills:ship-it", MigrationCategory.SKILLS, MigrationAction.CREATE)
        conflict = MigrationItem(
            "identity:soul",
            MigrationCategory.IDENTITY,
            MigrationAction.UPDATE,
            status=MigrationStatus.CONFLICT,
            conflict_policy=ConflictPolicy.REFUSE,
        )
        archive = MigrationItem(
            "archive:state-db",
            MigrationCategory.ARCHIVE,
            MigrationAction.ARCHIVE,
            status=MigrationStatus.ARCHIVE_ONLY,
        )
        plan = MigrationPlan(source=source, items=(ready, conflict, archive))

        self.assertEqual(plan.summary.total, 3)
        self.assertEqual(plan.summary.selected, 1)
        self.assertEqual(plan.summary.ready, 1)
        self.assertEqual(plan.summary.conflicts, 1)
        self.assertEqual(plan.summary.archive_only, 1)
        self.assertTrue(plan.has_blocking_conflicts)
        self.assertEqual([item.id for item in plan.apply_candidates], ["skills:ship-it"])
        self.assertEqual(set(plan.items_by_category()), {"skills", "identity", "archive"})

    def test_redacted_plan_serialization_masks_nested_secrets(self) -> None:
        from migration import MigrationAction, MigrationCategory, MigrationItem, MigrationPlan, MigrationSource, REDACTED

        source = MigrationSource.from_path("hermes", "C:/Users/test/.hermes", found=True)
        item = MigrationItem(
            id="mcp:demo",
            category=MigrationCategory.MCP,
            action=MigrationAction.CREATE,
            details={
                "server": {
                    "command": "npx",
                    "headers": {"Authorization": "Bearer abcdefghijklmnopqrstuvwxyz"},
                    "env": {"OPENAI_API_KEY": "sk-demo-secret", "SAFE_FLAG": "1"},
                }
            },
        )
        plan = MigrationPlan(source=source, items=(item,), metadata={"token": "abcdef0123456789abcdef0123456789"})
        redacted = plan.to_dict()
        raw_text = json.dumps(redacted)

        self.assertIn(REDACTED, raw_text)
        self.assertNotIn("sk-demo-secret", raw_text)
        self.assertNotIn("Bearer abcdef", raw_text)
        self.assertNotIn("abcdef0123456789abcdef0123456789", raw_text)
        self.assertIn("SAFE_FLAG", raw_text)
        self.assertIn("\"1\"", raw_text)

    def test_unredacted_serialization_is_available_for_internal_apply(self) -> None:
        from migration import MigrationAction, MigrationCategory, MigrationItem, MigrationPlan, MigrationSource

        source = MigrationSource.from_path("hermes", "C:/Users/test/.hermes", found=True)
        item = MigrationItem(
            id="api_keys:openai",
            category=MigrationCategory.API_KEYS,
            action=MigrationAction.UPDATE,
            details={"OPENAI_API_KEY": "sk-internal"},
        )
        plan = MigrationPlan(source=source, items=(item,))
        self.assertEqual(plan.to_dict(redact=False)["items"][0]["details"]["OPENAI_API_KEY"], "sk-internal")


if __name__ == "__main__":
    unittest.main(verbosity=2)
