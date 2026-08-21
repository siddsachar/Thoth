from types import SimpleNamespace


def test_default_local_llm_uses_capped_context(monkeypatch):
    import row_bot.models as models

    created = []
    monkeypatch.setattr(models, "_llm_instance", None)
    monkeypatch.setattr(models, "_current_model", "model:ollama:qwen3:14b")
    monkeypatch.setattr(models, "_num_ctx", 131_072)
    monkeypatch.setattr(models, "_local_context_mode", "fixed")
    monkeypatch.setattr(models, "is_cloud_model", lambda model_name: False)
    monkeypatch.setattr(models, "get_model_max_context", lambda model_name=None: 32_768)
    monkeypatch.setattr(models, "_chat_ollama", lambda **kwargs: created.append(kwargs) or object())

    models.get_llm()

    assert created == [{
        "model": "qwen3:14b",
        "num_ctx": 32_768,
    }]


def test_context_size_change_clears_override_llm_cache(monkeypatch):
    import row_bot.models as models

    created = []
    models._override_llm_cache[("qwen3:14b", 32_768)] = object()
    monkeypatch.setattr(models, "_current_model", "model:ollama:qwen3:14b")
    monkeypatch.setattr(models, "_local_context_mode", "fixed")
    monkeypatch.setattr(models, "is_cloud_model", lambda model_name: False)
    monkeypatch.setattr(models, "get_model_max_context", lambda model_name=None: None)
    monkeypatch.setattr(models, "_chat_ollama", lambda **kwargs: created.append(kwargs) or object())
    monkeypatch.setattr(models, "_save_settings", lambda settings: None)

    models.set_context_size(190_000)

    assert models._override_llm_cache == {}
    assert created[-1]["num_ctx"] == 190_000


def test_agent_graph_cache_uses_override_context(monkeypatch):
    import row_bot.agent as agent

    override = "model:ollama:qwen3:14b"
    context_calls = []

    def fake_context_size(model_name=None):
        context_calls.append(model_name)
        return 32_768 if model_name == override else 131_072

    def fake_create_react_agent(**kwargs):
        return SimpleNamespace(**kwargs)

    agent.clear_agent_cache()
    monkeypatch.setattr(agent, "get_current_model", lambda: "model:codex:gpt-5.5")
    monkeypatch.setattr(agent, "is_model_local", lambda model_name: model_name == override)
    monkeypatch.setattr(agent, "is_cloud_model", lambda model_name: False)
    monkeypatch.setattr(agent, "get_llm_for", lambda model_name: object())
    monkeypatch.setattr(agent, "get_context_size", fake_context_size)
    monkeypatch.setattr(agent, "_ensure_agent_mode_ready", lambda model_name: SimpleNamespace(
        provider_id="ollama",
        runtime_model="qwen3:14b",
        capability_source="test",
        confidence="high",
    ))
    monkeypatch.setattr(agent, "get_agent_system_prompt", lambda: "system")
    monkeypatch.setattr(agent, "create_react_agent", fake_create_react_agent)

    agent.get_agent_graph([], model_override=override)

    cache_key = next(iter(agent._agent_cache.keys()))
    assert override in context_calls
    assert "ctx:32768" in cache_key
    assert "model:model:ollama:qwen3:14b" in cache_key

    agent.clear_agent_cache()
