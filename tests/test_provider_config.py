import json

from providers.config import DEFAULT_ROUTE_PROFILES, load_provider_config, mask_provider_config, save_provider_config


def test_provider_config_normalizes_defaults(tmp_path):
    path = tmp_path / "providers.json"
    path.write_text(json.dumps({"quick_choices": "bad", "routes": []}))

    cfg = load_provider_config(path)

    assert cfg["version"] == 1
    assert cfg["quick_choices"] == []
    assert [route["id"] for route in cfg["routes"]][:5] == [route["id"] for route in DEFAULT_ROUTE_PROFILES]


def test_provider_config_atomic_save_and_masking(tmp_path):
    path = tmp_path / "providers.json"
    saved = save_provider_config({"providers": {"openai": {"api_key": "sk-test-secret"}}}, path)

    assert path.exists()
    assert json.loads(path.read_text())["version"] == 1
    assert saved["providers"]["openai"]["api_key"] == "sk-test-secret"

    masked = mask_provider_config(saved)
    assert masked["providers"]["openai"]["api_key"] == "****cret"