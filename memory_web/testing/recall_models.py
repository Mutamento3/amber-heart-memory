"""Only explicitly constructed synthetic recall roles; no network access."""

import time


class SyntheticRecallModels:
    def __init__(self, *, fail=(), delay=0):
        self.fail, self.delay, self.calls = set(fail), delay, []

    def _call(self, name, value):
        self.calls.append((name, value))
        if self.delay:
            time.sleep(self.delay)
        if name in self.fail:
            raise ValueError("synthetic model failure")

    def embed(self, text):
        self._call("embed", text)
        return [1, 0, 0]

    def clean_query(self, query, *, prompt=None):
        self._call("clean_query", query)
        return query

    def rerank(self, query, candidate_ids, *, documents=None):
        self._call("rerank", list(candidate_ids))
        return list(reversed(range(len(candidate_ids))))

    def select(self, query, candidate_ids, *, prompt=None):
        self._call("select", list(candidate_ids))
        return "1,2,3,4,5"
