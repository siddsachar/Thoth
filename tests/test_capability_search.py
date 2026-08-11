from __future__ import annotations

from row_bot.capability_search import CapabilityDocument, normalize_tokens, search_capabilities


def _doc(name: str, *, description: str = "", aliases: tuple[str, ...] = ()) -> CapabilityDocument:
    return CapabilityDocument(
        canonical_id=name,
        name=name,
        source="test",
        description=description,
        aliases=aliases,
    )


def test_normalize_tokens_handles_case_and_common_separators() -> None:
    assert normalize_tokens("sendGmail_message.file-name") == (
        "send",
        "gmail",
        "message",
        "file",
        "name",
    )


def test_search_uses_exact_prefix_substring_then_text_score() -> None:
    docs = [
        _doc("mail_search", description="find inbox messages"),
        _doc("search_mail", description="find inbox messages"),
        _doc("archived_search_mail", description="find inbox messages"),
        _doc("inbox_lookup", description="search mail messages in an inbox"),
    ]

    assert search_capabilities(docs, "mail_search")[0].name == "mail_search"
    assert [doc.name for doc in search_capabilities(docs, "search", limit=4)] == [
        "search_mail",
        "archived_search_mail",
        "mail_search",
        "inbox_lookup",
    ]


def test_alias_exact_match_and_deterministic_ties() -> None:
    docs = [
        _doc("plugin:beta:lookup", aliases=("lookup",), description="records"),
        _doc("plugin:alpha:find", description="records"),
        _doc("manual_lookup", aliases=("manual",), description="records"),
    ]

    assert search_capabilities(docs, "lookup", limit=5)[0].canonical_id == "plugin:beta:lookup"
    assert [doc.canonical_id for doc in search_capabilities(docs, "records", limit=5)] == sorted(
        doc.canonical_id for doc in docs
    )


def test_search_clamps_limit_and_empty_query_returns_no_catalog_dump() -> None:
    docs = [_doc(f"tool_{index}", description="shared token") for index in range(10)]

    assert len(search_capabilities(docs, "shared", limit=99)) == 5
    assert len(search_capabilities(docs, "shared", limit=0)) == 1
    assert search_capabilities(docs, "   ", limit=5) == []


def test_module_is_provider_and_network_independent() -> None:
    import row_bot.capability_search as module

    names = set(module.__dict__)
    assert not any(name.startswith("requests") or name.startswith("httpx") for name in names)
    assert not any("provider" in name.lower() or "telemetry" in name.lower() for name in names)
