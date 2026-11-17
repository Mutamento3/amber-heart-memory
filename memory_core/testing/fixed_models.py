"""Return only predeclared responses. Unknown input fails instead of calling a model."""

from copy import deepcopy
import json
from pathlib import Path


class MissingFixture(ValueError):
    pass


class FixedModels:
    def __init__(self, fixtures):
        self.fixtures = deepcopy(fixtures)
        self.calls = []

    @classmethod
    def from_file(cls, path):
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def _response(self, stage, request):
        self.calls.append((stage, request))
        try:
            response = deepcopy(self.fixtures[stage][request])
        except KeyError as exc:
            raise MissingFixture(f"missing fixed response for stage {stage}") from exc
        if isinstance(response, dict) and "error" in response:
            errors = {"timeout": TimeoutError, "invalid": ValueError}
            if response["error"] not in errors:
                raise ValueError("unknown fixed error")
            raise errors[response["error"]]("fixed test failure")
        return response

    def extract(self, material):
        return self._response("extract", material)

    def embed(self, text):
        return self._response("embed", text)

    def clean_query(self, query, *, prompt=None):
        request = query if prompt is None else json.dumps([query,prompt],ensure_ascii=False)
        return self._response("clean_query", request)

    def rerank(self, query, candidate_ids, *, documents=None):
        request = [query, list(candidate_ids)]
        if documents is not None:
            request.append(list(documents))
        return self._response("rerank", json.dumps(request, ensure_ascii=False))

    def select(self, query, candidate_ids, *, prompt=None):
        request = [query, list(candidate_ids)]
        if prompt is not None:
            request.append(prompt)
        return self._response("select", json.dumps(request, ensure_ascii=False))
