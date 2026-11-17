"""Provider contracts only: no vendor SDK, network client or model defaults."""

from typing import Protocol, Sequence


class Extractor(Protocol):
    def extract(self, material: str) -> str: ...


class Embedder(Protocol):
    def embed(self, text: str) -> list[float]: ...


def embed_for(embedder: Embedder, text: str, *, purpose: str, title: str | None = None) -> list[float]:
    """Pass retrieval context to adapters that support it and retain text-only providers."""
    contextual_embed = getattr(embedder, "embed_for", None)
    if contextual_embed is not None:
        return contextual_embed(text, purpose=purpose, title=title)
    return embedder.embed(text)


class KeywordCleaner(Protocol):
    def clean_query(self, query: str, *, prompt: str) -> str: ...


class DocumentReranker(Protocol):
    """Return zero-based document positions in descending relevance order."""
    def rerank(self, query: str, candidate_ids: Sequence[int], *, documents: Sequence[str]) -> list[int]: ...


class FinalSelector(Protocol):
    """Return comma-separated one-based positions from the rendered prompt."""
    def select(self, query: str, candidate_ids: Sequence[int], *, prompt: str) -> str: ...
