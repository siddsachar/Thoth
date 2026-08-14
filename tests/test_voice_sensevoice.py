from __future__ import annotations

import json
from pathlib import Path

import pytest

from row_bot.voice import VoiceService
from row_bot.voice import local_provider
from row_bot.voice.local_provider import (
    DEFAULT_FUNASR_MODEL,
    LocalFunASRProvider,
    SenseVoiceUnavailableError,
    install_sensevoice_model,
    sensevoice_runtime_status,
)


@pytest.fixture(autouse=True)
def _isolated_voice_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import row_bot.voice as voice

    data_dir = tmp_path / "row-bot-data"
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(data_dir))
    monkeypatch.setattr(voice, "_DATA_DIR", data_dir)
    monkeypatch.setattr(voice, "_VOICE_SETTINGS_FILE", data_dir / "voice_settings.json")
    monkeypatch.setattr(voice, "_WHISPER_CACHE_DIR", data_dir / "cache" / "whisper")


def _snapshot(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "config.yaml").write_text("model: SenseVoiceSmall\n", encoding="utf-8")
    (path / "model.pt").write_bytes(b"local-weights")
    return path


def _packages_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_provider, "sensevoice_platform_support", lambda: (True, ""))
    monkeypatch.setattr(local_provider, "missing_funasr_packages", lambda: ())


def test_sensevoice_readiness_startup_and_transcription_do_not_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _packages_ready(monkeypatch)
    monkeypatch.setattr(
        local_provider,
        "install_sensevoice_model",
        lambda: pytest.fail("ordinary SenseVoice use must not start a download"),
    )
    model_factory_calls = []
    provider = LocalFunASRProvider(
        model_factory=lambda **kwargs: model_factory_calls.append(kwargs)
    )

    status = provider.runtime_status()

    assert status.state == "model_missing"
    assert status.ready is False
    with pytest.raises(SenseVoiceUnavailableError, match="explicitly"):
        provider.ensure_model()
    with pytest.raises(SenseVoiceUnavailableError, match="explicitly"):
        provider.transcribe_bytes(b"\x00\x00")

    service = VoiceService()
    service._sensevoice_model_path = ""
    service.stt_model = "local-funasr-sensevoice"
    with pytest.raises(SenseVoiceUnavailableError, match="explicitly"):
        service._ensure_selected_stt_model()
    with pytest.raises(SenseVoiceUnavailableError, match="explicitly"):
        service.transcribe_bytes(b"\x00\x00")

    assert model_factory_calls == []


def test_explicit_sensevoice_install_uses_modelscope_snapshot_and_verifies_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _packages_ready(monkeypatch)
    calls = []

    def _download(model_id: str, *, cache_dir: str) -> str:
        calls.append((model_id, cache_dir))
        return str(_snapshot(Path(cache_dir) / "hub" / "models" / "iic" / "SenseVoiceSmall"))

    cache_dir = tmp_path / "sensevoice"
    installed = install_sensevoice_model(
        cache_dir=cache_dir,
        snapshot_downloader=_download,
    )

    assert calls == [(DEFAULT_FUNASR_MODEL, str(cache_dir))]
    assert installed == (
        cache_dir / "hub" / "models" / "iic" / "SenseVoiceSmall"
    ).resolve()


def test_voice_service_persists_verified_sensevoice_path_for_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import row_bot.voice as voice

    model_path = _snapshot(tmp_path / "cache" / "sensevoice" / "snapshot")
    monkeypatch.setattr(voice, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(voice, "_VOICE_SETTINGS_FILE", tmp_path / "voice_settings.json")
    monkeypatch.setattr(local_provider, "install_sensevoice_model", lambda: model_path)

    service = VoiceService()
    installed = service.install_sensevoice_model()
    restarted = VoiceService()

    saved = json.loads((tmp_path / "voice_settings.json").read_text(encoding="utf-8"))
    assert installed == model_path
    assert saved["sensevoice_model_path"] == str(model_path)
    assert restarted.sensevoice_model_path == str(model_path)


def test_cached_sensevoice_startup_passes_only_verified_local_path_to_automodel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _packages_ready(monkeypatch)
    cache_dir = tmp_path / "sensevoice"
    model_path = _snapshot(cache_dir / "snapshot")
    calls = []
    model = object()

    def _factory(**kwargs):
        calls.append(kwargs)
        return model

    provider = LocalFunASRProvider(
        model_path=model_path,
        cache_dir=cache_dir,
        model_factory=_factory,
    )

    assert provider.ensure_model() is model
    assert calls == [
        {
            "model": str(model_path.resolve()),
            "device": "cpu",
            "disable_update": True,
        }
    ]


@pytest.mark.parametrize(
    ("model_path", "expected_state"),
    [
        (None, "model_missing"),
        ("missing", "model_invalid"),
        ("corrupt", "model_invalid"),
    ],
)
def test_missing_or_corrupt_sensevoice_snapshots_are_recoverable(
    model_path: str | None,
    expected_state: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _packages_ready(monkeypatch)
    cache_dir = tmp_path / "sensevoice"
    candidate = None if model_path is None else cache_dir / model_path
    if model_path == "corrupt":
        candidate.mkdir(parents=True)
        (candidate / "config.yaml").write_text("model: SenseVoiceSmall\n")

    status = sensevoice_runtime_status(candidate, cache_dir=cache_dir)

    assert status.state == expected_state
    assert status.ready is False
    assert "install" in status.reason.lower() or "reinstall" in status.reason.lower()


def test_sensevoice_package_probe_reports_missing_runtime_without_importing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probed = []

    def _find_spec(module: str):
        probed.append(module)
        return None

    monkeypatch.setattr(local_provider.importlib.util, "find_spec", _find_spec)
    monkeypatch.setattr(local_provider, "sensevoice_platform_support", lambda: (True, ""))

    status = sensevoice_runtime_status(None)

    assert status.state == "packages_missing"
    assert status.missing_packages == local_provider.FUNASR_REQUIRED_MODULES
    assert probed == list(local_provider.FUNASR_REQUIRED_MODULES)


def test_sensevoice_reports_intel_macos_as_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(local_provider.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(local_provider.platform, "machine", lambda: "x86_64")

    status = sensevoice_runtime_status(None)

    assert status.state == "unsupported"
    assert status.ready is False
    assert "Intel macOS" in status.reason


def test_whisper_transcription_path_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    service = VoiceService()
    service.stt_model = "local-whisper-small"
    calls = []

    def _transcribe(audio_bytes: bytes, *, allow_download: bool) -> str:
        calls.append((audio_bytes, allow_download))
        return "whisper text"

    monkeypatch.setattr(service, "transcribe_pcm16", _transcribe)

    assert service.transcribe_bytes(b"pcm") == "whisper text"
    assert calls == [(b"pcm", True)]
