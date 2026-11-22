"""Short-lived, memory-only reasoning for tool rounds whose client drops it.

DeepSeek's thinking mode rejects a tool continuation unless each assistant
tool-call message of the current turn carries its `reasoning_content`. Some
OpenAI-compatible clients (e.g. Operit's generic provider) do not send it back.
The proxy already reads every reply it forwards, so it keeps that text in
process memory for a while and restores it only where the client left it out.
Nothing here is written to Postgres; a restart simply forgets it.
"""

import threading
import time

TTL_SECONDS = 30 * 60
MAX_TURNS = 512


class ReasoningCache:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.turns = {}
        self.lock = threading.Lock()

    def _prune(self, now):
        expired = [key for key, (stamp, _) in self.turns.items() if now - stamp > TTL_SECONDS]
        for key in expired:
            del self.turns[key]
        while len(self.turns) > MAX_TURNS:
            del self.turns[min(self.turns, key=lambda key: self.turns[key][0])]

    def remember(self, space, turn_id, step_index, reasoning):
        """Keep the reasoning of one reply that ended in tool calls."""
        if not reasoning:
            return
        now = self.clock()
        with self.lock:
            stamp, steps = self.turns.get((space, turn_id), (now, {}))
            steps[step_index] = reasoning
            self.turns[(space, turn_id)] = (now, steps)
            self._prune(now)

    def fill(self, space, turn_id, step_index, messages):
        """Restore missing reasoning on this turn's assistant tool-call messages, in order."""
        with self.lock:
            self._prune(self.clock())
            entry = self.turns.get((space, turn_id))
            if not entry:
                return 0
            steps = entry[1]
            known = [steps[k] for k in sorted(steps) if k < step_index]
        start = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1) + 1
        calls = [m for m in messages[start:] if m.get("role") == "assistant" and m.get("tool_calls")]
        # Each earlier tool round of this turn produced one such message; align from the end.
        restored = 0
        for message, reasoning in zip(reversed(calls), reversed(known)):
            if not message.get("reasoning_content"):
                message["reasoning_content"] = reasoning
                restored += 1
        return restored
