"""Small deterministic lexical search shared by tool and skill discovery."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import math
import re
from typing import Iterable, Sequence


_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NON_ALNUM = re.compile(r"[^A-Za-z0-9]+")


@dataclass(frozen=True, slots=True)
class CapabilityDocument:
    """Provider-neutral searchable metadata for one authorized capability."""

    canonical_id: str
    name: str
    source: str
    description: str = ""
    tags: tuple[str, ...] = ()
    parameter_names: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()


def normalize_tokens(value: str) -> tuple[str, ...]:
    """Split camelCase, snake_case, punctuation, and case into stable tokens."""

    expanded = _CAMEL_BOUNDARY.sub(" ", str(value or ""))
    return tuple(token.lower() for token in _NON_ALNUM.split(expanded) if token)


def _document_tokens(document: CapabilityDocument) -> tuple[str, ...]:
    fields: Iterable[str] = (
        document.canonical_id,
        document.name,
        document.source,
        document.description,
        *document.tags,
        *document.parameter_names,
        *document.aliases,
    )
    return tuple(token for field in fields for token in normalize_tokens(field))


def _name_match_tier(document: CapabilityDocument, query: str) -> int:
    query_folded = query.casefold()
    names = (document.name, document.canonical_id, *document.aliases)
    folded = tuple(str(name or "").casefold() for name in names if str(name or "").strip())
    if query_folded in folded:
        return 0

    primary_names = (document.canonical_id.casefold(), document.name.casefold())
    if any(name.startswith(query_folded) for name in primary_names):
        return 1
    if any(query_folded in name for name in primary_names):
        return 2
    return 3


def search_capabilities(
    documents: Sequence[CapabilityDocument],
    query: str,
    *,
    limit: int = 3,
) -> list[CapabilityDocument]:
    """Rank documents locally using explicit name tiers then BM25-style text score."""

    query_text = str(query or "").strip()
    if not query_text:
        return []
    bounded_limit = min(5, max(1, int(limit or 1)))
    query_tokens = normalize_tokens(query_text)
    if not query_tokens:
        return []

    tokenized = [_document_tokens(document) for document in documents]
    document_count = len(tokenized)
    if not document_count:
        return []
    average_length = sum(len(tokens) for tokens in tokenized) / document_count or 1.0
    document_frequency = Counter(
        token
        for tokens in tokenized
        for token in set(tokens)
    )

    ranked: list[tuple[tuple[object, ...], CapabilityDocument]] = []
    for document, tokens in zip(documents, tokenized, strict=True):
        tier = _name_match_tier(document, query_text)
        frequencies = Counter(tokens)
        score = 0.0
        for token in query_tokens:
            frequency = frequencies.get(token, 0)
            if not frequency:
                continue
            inverse_frequency = math.log(
                1.0 + (document_count - document_frequency[token] + 0.5)
                / (document_frequency[token] + 0.5)
            )
            denominator = frequency + 1.2 * (
                0.25 + 0.75 * (len(tokens) / average_length)
            )
            score += inverse_frequency * (frequency * 2.2) / denominator

        if tier == 3 and score <= 0:
            continue
        rank_key = (
            tier,
            -score if tier == 3 else 0.0,
            document.canonical_id.casefold(),
            document.canonical_id,
        )
        ranked.append((rank_key, document))

    ranked.sort(key=lambda item: item[0])
    return [document for _rank, document in ranked[:bounded_limit]]
