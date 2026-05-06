"""Focused tests for Phase 2 migration source detection."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path


class MigrationDetectionTests(unittest.TestCase):
    def test_detects_realistic_hermes_home(self) -> None:
        from migration import MigrationProvider, detect_hermes_source
        from migration.fixtures import create_realistic_hermes_home

        with tempfile.TemporaryDirectory() as temp_dir:
            source = create_realistic_hermes_home(Path(temp_dir) / ".hermes")
            scan = detect_hermes_source(source)

        self.assertEqual(scan.source.provider, MigrationProvider.HERMES)
        self.assertTrue(scan.source.found)
        self.assertEqual(scan.source.confidence, "high")
        self.assertIn("config.yaml", scan.source.discovered_files)
        self.assertIn("memories/MEMORY.md", scan.source.discovered_files)
        self.assertIn("plugins", scan.source.discovered_files)
        self.assertGreaterEqual(scan.metadata["skill_count"], 2)
        self.assertIn("OPENAI_API_KEY", scan.metadata["env_keys"])
        self.assertGreaterEqual(scan.summary.archive_only, 3)
        self.assertGreaterEqual(scan.summary.sensitive, 2)

    def test_detects_realistic_openclaw_home(self) -> None:
        from migration import MigrationProvider, detect_openclaw_source
        from migration.fixtures import create_realistic_openclaw_home

        with tempfile.TemporaryDirectory() as temp_dir:
            source = create_realistic_openclaw_home(Path(temp_dir) / ".openclaw")
            scan = detect_openclaw_source(source)

        self.assertEqual(scan.source.provider, MigrationProvider.OPENCLAW)
        self.assertTrue(scan.source.found)
        self.assertEqual(scan.source.confidence, "high")
        self.assertIn("openclaw.json", scan.source.discovered_files)
        self.assertIn("workspace/AGENTS.md", scan.source.discovered_files)
        self.assertIn("workspace/skills", scan.source.discovered_files)
        self.assertIn("exec-approvals.json", scan.source.discovered_files)
        self.assertGreaterEqual(scan.metadata["workspace_skill_count"], 2)
        self.assertGreaterEqual(scan.metadata["shared_skill_count"], 1)
        self.assertGreaterEqual(scan.summary.archive_only, 4)
        self.assertGreaterEqual(scan.summary.risky, 1)

    def test_openclaw_legacy_directory_warns(self) -> None:
        from migration import detect_openclaw_source
        from migration.fixtures import create_realistic_openclaw_home

        with tempfile.TemporaryDirectory() as temp_dir:
            source = create_realistic_openclaw_home(Path(temp_dir) / ".clawdbot")
            scan = detect_openclaw_source(source)

        self.assertTrue(scan.source.found)
        self.assertTrue(any("legacy OpenClaw" in warning for warning in scan.source.warnings))

    def test_wrong_provider_source_does_not_partially_match_generic_folders(self) -> None:
        from migration import detect_hermes_source, detect_openclaw_source
        from migration.fixtures import create_realistic_hermes_home, create_realistic_openclaw_home

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            openclaw_source = create_realistic_openclaw_home(root / ".openclaw")
            hermes_source = create_realistic_hermes_home(root / ".hermes")
            hermes_scan = detect_hermes_source(openclaw_source)
            openclaw_scan = detect_openclaw_source(hermes_source)

        self.assertFalse(hermes_scan.source.found)
        self.assertFalse(openclaw_scan.source.found)
        self.assertEqual(hermes_scan.metadata["error"], "provider_mismatch")
        self.assertEqual(openclaw_scan.metadata["error"], "provider_mismatch")
        self.assertTrue(any("Choose OpenClaw" in warning for warning in hermes_scan.source.warnings))
        self.assertTrue(any("Choose Hermes" in warning for warning in openclaw_scan.source.warnings))

    def test_missing_sources_are_low_confidence(self) -> None:
        from migration import detect_hermes_source, detect_openclaw_source

        with tempfile.TemporaryDirectory() as temp_dir:
            missing_hermes = detect_hermes_source(Path(temp_dir) / "missing-hermes")
            missing_openclaw = detect_openclaw_source(Path(temp_dir) / "missing-openclaw")

        self.assertFalse(missing_hermes.source.found)
        self.assertEqual(missing_hermes.source.confidence, "low")
        self.assertFalse(missing_openclaw.source.found)
        self.assertEqual(missing_openclaw.source.confidence, "low")
        self.assertIn("source directory does not exist", missing_openclaw.source.warnings)

    def test_detection_serialization_redacts_metadata_secrets(self) -> None:
        from migration import detect_hermes_source
        from migration.fixtures import create_realistic_hermes_home

        with tempfile.TemporaryDirectory() as temp_dir:
            source = create_realistic_hermes_home(Path(temp_dir) / ".hermes")
            scan = detect_hermes_source(source)
            payload = scan.to_dict()

        raw_text = json.dumps(payload)
        self.assertNotIn("sk-hermes-three-month-user", raw_text)
        self.assertNotIn("sk-ant-hermes-example", raw_text)
        self.assertIn("OPENAI_API_KEY", raw_text)
        self.assertTrue(payload["read_only"])

    def test_detection_does_not_modify_source_tree(self) -> None:
        from migration import detect_openclaw_source
        from migration.fixtures import create_realistic_openclaw_home

        with tempfile.TemporaryDirectory() as temp_dir:
            source = create_realistic_openclaw_home(Path(temp_dir) / ".openclaw")
            before = _snapshot_tree(source)
            scan = detect_openclaw_source(source)
            after = _snapshot_tree(source)

        self.assertTrue(scan.read_only)
        self.assertEqual(before, after)


def _snapshot_tree(root: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            snapshot[str(path.relative_to(root)).replace("\\", "/")] = path.read_text(encoding="utf-8")
    return snapshot


if __name__ == "__main__":
    unittest.main(verbosity=2)
