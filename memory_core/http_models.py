"""Implement the four recall protocols without importing a private gateway."""

import asyncio
import math

from .completion_model import CompletionModel, no_thinking_options
from .http_transport import ModelCallError, post_json
from .model_config import RecallModelsConfig
from .rerank_input import ranking_query


class EmbeddingDimensionError(ValueError):
    pass


class HTTPRecallModels:
    def __init__(
        self,
        config: RecallModelsConfig,
        dimensions,
        *,
        main_url=None,
        main_key=None,
        transport=None,
    ):
        self.config, self.dimensions, self.transport = config, dimensions, transport
        self.embedding = config.embedding.resolve(main_url, main_key)
        self.cleaner = config.cleaner.resolve(main_url, main_key)
        self.selector = config.selector.resolve(main_url, main_key)
        # The document reranker uses its own endpoint and credentials.
        self.reranker = config.reranker

    @staticmethod
    def require(role, name):
        if not role.configured:
            raise ModelCallError(name + "_model_unconfigured")

    def embed_for(self, text, *, purpose, title=None):
        # Older providers only need the original text. Keep their call path intact.
        if "gemini-embedding-2" in (self.embedding.model or "").lower():
            return self.embed(text, purpose=purpose, title=title)
        return self.embed(text)

    def embed(self, text, *, purpose="document", title=None):
        self.require(self.embedding, "embedding")
        model_name = self.embedding.model.lower()
        supports_dimensions = "gemini-embedding" in model_name or "text-embedding-3" in model_name
        if purpose not in ("query", "document"):
            raise ValueError("嵌入用途必须是 query 或 document")
        if "gemini-embedding-2" in model_name:
            if purpose == "query":
                text = f"task: search result | query: {text}"
            else:
                text = f"title: {title or 'none'} | text: {text}"
        payload = dict(model=self.embedding.model, input=text, encoding_format="float")
        if supports_dimensions:
            payload["dimensions"] = self.dimensions
        value = asyncio.run(
            post_json(
                self.embedding.base_url,
                self.embedding.api_key,
                "embeddings",
                payload,
                role="embedding",
                timeout=self.config.timeout_seconds,
                transport=self.transport,
            )
        )
        rows = value.get("data")
        if (
            not isinstance(rows, list)
            or len(rows) != 1
            or not isinstance(rows[0], dict)
            or type(rows[0].get("index")) is not int
            or rows[0].get("index") != 0
        ):
            raise ModelCallError("invalid_embedding_response")
        vector = rows[0].get("embedding")
        if not isinstance(vector, list):
            raise ModelCallError("invalid_embedding_response")
        oversized = supports_dimensions and len(vector) > self.dimensions
        if len(vector) != self.dimensions and not oversized:
            raise EmbeddingDimensionError(
                f"嵌入返回 {len(vector)} 维，记忆库安装为 {self.dimensions} 维；请核对型号或安装维度"
            )
        if any(type(n) not in (int, float) or not math.isfinite(n) for n in vector):
            raise ModelCallError("invalid_embedding_vector")
        if oversized:
            vector = vector[:self.dimensions]
        if not any(vector):
            raise ModelCallError("invalid_embedding_vector")
        if oversized:
            # The prefix of a unit vector must be normalized again after truncation.
            scale = max(map(abs, vector))
            vector = [n / scale for n in vector]
            length = math.hypot(*vector)
            vector = [n / length for n in vector]
        return vector

    def verify_dimensions(self):
        if self.embedding.configured:
            try:
                self.embed_for("embedding dimension verification", purpose="query")
            except EmbeddingDimensionError:
                raise
            except ModelCallError as exc:
                raise ValueError("嵌入启动核对失败：" + exc.code) from None

    def _chat(self, role, name, prompt, query):
        self.require(role, name)
        options = dict(max_tokens=60, temperature=0, **no_thinking_options(role.base_url))
        model = CompletionModel(
            endpoint=role.base_url,
            key=role.api_key,
            model=role.model,
            role=name,
            transport=self.transport,
            timeout=self.config.timeout_seconds,
            request_options=options,
        )
        return model.complete_sync(prompt, query)["content"]

    def clean_query(self, query, *, prompt):
        return self._chat(self.cleaner, "keyword", prompt, query)

    def select(self, query, candidate_ids, *, prompt):
        return self._chat(self.selector, "final_rerank", prompt, query)

    def prepare_rerank_query(self, query):
        if self.config.document_rerank_format == "voyage":
            return ranking_query(query)
        return query

    def rerank(self, query, candidate_ids, *, documents):
        self.require(self.reranker, "document_rerank")
        if len(documents) != len(candidate_ids) or not 1 <= len(documents) <= 1000:
            raise ModelCallError("invalid_document_rerank_input")
        payload = dict(model=self.reranker.model, query=query, documents=list(documents))
        if self.config.document_rerank_format == "voyage":
            payload.update(top_k=len(documents), return_documents=False)
            results_key = "data"
        else:
            payload["top_n"] = len(documents)
            results_key = "results"
        value = asyncio.run(
            post_json(
                self.reranker.base_url,
                self.reranker.api_key,
                "rerank",
                payload,
                role="document_rerank",
                timeout=self.config.timeout_seconds,
                transport=self.transport,
            )
        )
        rows = value.get(results_key)
        if not isinstance(rows, list):
            raise ModelCallError("document_rerank_format_mismatch")
        if len(rows) != len(documents):
            raise ModelCallError("invalid_document_rerank_response")
        positions = []
        for row in rows:
            if not isinstance(row, dict):
                raise ModelCallError("invalid_document_rerank_response")
            position, score = row.get("index"), row.get("relevance_score")
            if (
                type(position) is not int
                or not 0 <= position < len(documents)
                or position in positions
                or type(score) not in (int, float)
                or not math.isfinite(score)
            ):
                raise ModelCallError("invalid_document_rerank_response")
            positions.append(position)
        return positions
