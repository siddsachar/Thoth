"""Focused tests for keyring-backed API key storage."""

from __future__ import annotations

import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path


class FakeKeyring:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, account: str) -> str | None:
        if self.fail:
            raise RuntimeError("keyring unavailable")
        return self.values.get((service, account))

    def set_password(self, service: str, account: str, value: str) -> None:
        if self.fail:
            raise RuntimeError("keyring unavailable")
        self.values[(service, account)] = value

    def delete_password(self, service: str, account: str) -> None:
        if self.fail:
            raise RuntimeError("keyring unavailable")
        self.values.pop((service, account), None)


class ApiKeyStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_data_dir = os.environ.get("THOTH_DATA_DIR")
        os.environ["THOTH_DATA_DIR"] = self.temp_dir.name
        import secret_store
        import api_keys
        from plugins import state as plugin_state
        self.secret_store = importlib.reload(secret_store)
        self.api_keys = importlib.reload(api_keys)
        self.plugin_state = importlib.reload(plugin_state)
        self.backend = FakeKeyring()
        self.secret_store._set_backend_for_tests(self.backend)

    def tearDown(self) -> None:
        self.secret_store._set_backend_for_tests(None)
        if self.old_data_dir is None:
            os.environ.pop("THOTH_DATA_DIR", None)
        else:
            os.environ["THOTH_DATA_DIR"] = self.old_data_dir
        for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
            os.environ.pop(key, None)
        self.temp_dir.cleanup()

    def test_set_key_writes_keyring_and_metadata_only(self) -> None:
        self.api_keys.set_key("OPENAI_API_KEY", "sk-test-secret-1234")

        self.assertEqual(self.api_keys.get_key("OPENAI_API_KEY"), "sk-test-secret-1234")
        metadata = json.loads(Path(self.api_keys.KEYS_PATH).read_text(encoding="utf-8"))
        self.assertEqual(metadata["version"], 2)
        self.assertEqual(metadata["keys"]["OPENAI_API_KEY"]["fingerprint"], "****1234")
        self.assertNotIn("sk-test-secret-1234", json.dumps(metadata))

    def test_legacy_plaintext_file_is_migrated_to_keyring(self) -> None:
        Path(self.api_keys.KEYS_PATH).write_text(
            json.dumps({"OPENAI_API_KEY": "sk-legacy-secret"}),
            encoding="utf-8",
        )

        keys = self.api_keys._load_keys()
        metadata = json.loads(Path(self.api_keys.KEYS_PATH).read_text(encoding="utf-8"))

        self.assertEqual(keys["OPENAI_API_KEY"], "sk-legacy-secret")
        self.assertEqual(metadata["version"], 2)
        self.assertNotIn("sk-legacy-secret", json.dumps(metadata))

    def test_legacy_plaintext_remains_available_when_keyring_fails(self) -> None:
        self.secret_store._set_backend_for_tests(FakeKeyring(fail=True))
        Path(self.api_keys.KEYS_PATH).write_text(
            json.dumps({"ANTHROPIC_API_KEY": "sk-ant-legacy"}),
            encoding="utf-8",
        )

        self.assertEqual(self.api_keys.get_key("ANTHROPIC_API_KEY"), "sk-ant-legacy")
        self.assertIn("legacy plaintext", self.api_keys.get_storage_warning())
        raw = json.loads(Path(self.api_keys.KEYS_PATH).read_text(encoding="utf-8"))
        self.assertEqual(raw["ANTHROPIC_API_KEY"], "sk-ant-legacy")

    def test_new_key_is_session_only_when_keyring_fails(self) -> None:
        self.secret_store._set_backend_for_tests(FakeKeyring(fail=True))

        self.api_keys.set_key("OPENAI_API_KEY", "sk-session-only")

        self.assertEqual(self.api_keys.get_key("OPENAI_API_KEY"), "sk-session-only")
        self.assertFalse(Path(self.api_keys.KEYS_PATH).exists())
        self.assertIn("session only", self.api_keys.get_storage_warning())

    def test_delete_key_removes_keyring_metadata_and_environment(self) -> None:
        self.api_keys.set_key("OPENAI_API_KEY", "sk-delete-me")
        self.api_keys.delete_key("OPENAI_API_KEY")

        self.assertEqual(self.api_keys.get_key("OPENAI_API_KEY"), "")
        self.assertNotIn("OPENAI_API_KEY", os.environ)
        metadata = json.loads(Path(self.api_keys.KEYS_PATH).read_text(encoding="utf-8"))
        self.assertNotIn("OPENAI_API_KEY", metadata.get("keys", {}))

    def test_plugin_secret_writes_keyring_and_metadata_only(self) -> None:
        self.plugin_state.set_plugin_secret("plugin-a", "API_KEY", "plug-secret-1234")

        self.assertEqual(self.plugin_state.get_plugin_secret("plugin-a", "API_KEY"), "plug-secret-1234")
        metadata = json.loads(Path(self.plugin_state._SECRETS_PATH).read_text(encoding="utf-8"))
        self.assertEqual(metadata["version"], 2)
        self.assertEqual(metadata["plugins"]["plugin-a"]["API_KEY"]["fingerprint"], "****1234")
        self.assertNotIn("plug-secret-1234", json.dumps(metadata))

    def test_plugin_legacy_plaintext_file_is_migrated_to_keyring(self) -> None:
        Path(self.plugin_state._SECRETS_PATH).write_text(
            json.dumps({"plugin-a": {"API_KEY": "plug-legacy-secret"}}),
            encoding="utf-8",
        )
        self.plugin_state._reset()

        self.assertEqual(self.plugin_state.get_plugin_secret("plugin-a", "API_KEY"), "plug-legacy-secret")
        metadata = json.loads(Path(self.plugin_state._SECRETS_PATH).read_text(encoding="utf-8"))

        self.assertEqual(metadata["version"], 2)
        self.assertNotIn("plug-legacy-secret", json.dumps(metadata))

    def test_plugin_legacy_plaintext_remains_available_when_keyring_fails(self) -> None:
        self.secret_store._set_backend_for_tests(FakeKeyring(fail=True))
        Path(self.plugin_state._SECRETS_PATH).write_text(
            json.dumps({"plugin-a": {"API_KEY": "plug-legacy-secret"}}),
            encoding="utf-8",
        )
        self.plugin_state._reset()

        self.assertEqual(self.plugin_state.get_plugin_secret("plugin-a", "API_KEY"), "plug-legacy-secret")
        raw = json.loads(Path(self.plugin_state._SECRETS_PATH).read_text(encoding="utf-8"))
        self.assertEqual(raw["plugin-a"]["API_KEY"], "plug-legacy-secret")


if __name__ == "__main__":
    unittest.main()
