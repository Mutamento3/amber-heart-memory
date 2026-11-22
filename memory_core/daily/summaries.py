"""Versioned whole-turn summaries; model calls are deliberately outside this store."""

from contextlib import contextmanager
from datetime import datetime, timezone

from psycopg.types.json import Jsonb

from ..conversations import chat_text as text
from ..storage.clock import format_timestamp
from ..conversations.conversation_material import dialogue_input
from ..conversations.conversation_rounds import fingerprint
from ..conversations.conversations import ConversationStore
from ..storage.db import background_lock, connect
from ..storage.schema import SUMMARY_VERSION
from ..storage.space import read_policy
from .summary_policy import SummarySettings, summary_batch


def active_summaries(connection, space, day):
    """Reject stale coverage at read time too; valid leaves survive bad rollups."""
    rows = connection.execute(
        """SELECT * FROM memory_core.conversation_summaries
        WHERE bot_id=%s AND business_date=%s AND content_deleted_at IS NULL ORDER BY id""",
        (space, day),
    ).fetchall()
    covered = connection.execute(
        """SELECT st.summary_id,st.turn_id,t.history_state,t.content_deleted_at
        FROM memory_core.conversation_summary_turns st JOIN memory_core.conversation_turns t
        ON (t.id,t.bot_id)=(st.turn_id,st.bot_id) WHERE st.bot_id=%s AND st.business_date=%s""",
        (space, day),
    ).fetchall()
    leaves = {}
    for row in rows:
        matches = [r for r in covered if r["summary_id"] == row["id"]]
        if (
            row["level"] == 1
            and matches
            and all(
                r["history_state"] == "current" and r["content_deleted_at"] is None
                for r in matches
            )
        ):
            leaves[row["id"]] = dict(
                row, covered_turn_ids=[r["turn_id"] for r in matches]
            )
    merged, output = set(), []
    for row in rows:
        if row["level"] != 2:
            continue
        ids = [
            row[k]
            for k in ("first_summary_id", "second_summary_id", "third_summary_id")
        ]
        if all(i in leaves for i in ids) and not merged.intersection(ids):
            output.append(
                dict(
                    row,
                    covered_turn_ids=[
                        t for i in ids for t in leaves[i]["covered_turn_ids"]
                    ],
                )
            )
            merged.update(ids)
    output += [row for key, row in leaves.items() if key not in merged]
    return sorted(output, key=lambda row: (datetime.fromisoformat(row["start_at"]), row["id"]))


def uncovered_turns(connection, space, day):
    return connection.execute(
        """SELECT t.* FROM memory_core.conversation_turns t
        WHERE t.bot_id=%s AND t.business_date=%s AND t.history_state='current' AND t.content_deleted_at IS NULL
        AND t.generation_state IN ('complete','failed','incomplete') AND NOT EXISTS
            (SELECT 1 FROM memory_core.conversation_summary_turns st JOIN memory_core.conversation_summaries s
             ON (s.id,s.bot_id)=(st.summary_id,st.bot_id) WHERE st.bot_id=t.bot_id AND st.turn_id=t.id
             AND s.content_deleted_at IS NULL)
        ORDER BY t.position_id""",
        (space, day),
    ).fetchall()


class SummaryStore(ConversationStore):
    def __init__(self, dsn, space_id, settings=None):
        super().__init__(dsn, space_id)
        if self.schema_version < SUMMARY_VERSION:
            raise ValueError("及时摘要需要先升级记忆核心到版本 12")
        self.summary_settings = settings or SummarySettings()

    @contextmanager
    def job_lock(self):
        """Session lock survives short commits but vanishes on worker death."""
        with background_lock(self.dsn, "conversation-summary:" + self.space_id) as guard:
            yield guard is not None

    def _input(self, connection, turns, policy):
        return dialogue_input(connection, self.space_id, turns, policy, self.settings)

    def _rollup_batch(self, c, day):
        leaves = c.execute(
            """SELECT s.*,EXISTS (SELECT 1 FROM memory_core.conversation_summaries previous
                WHERE previous.bot_id=s.bot_id AND previous.first_summary_id=s.id) AS was_first
            FROM memory_core.conversation_summaries s WHERE s.bot_id=%s AND s.business_date=%s
            AND s.level=1 AND s.merged_into_id IS NULL AND s.content_deleted_at IS NULL ORDER BY s.id""",
            (self.space_id, day),
        ).fetchall()
        # A deleted rollup still owns its first-summary key. Its surviving
        # leaves can be read immediately and join a later batch with a new key.
        start = next((i for i, leaf in enumerate(leaves) if not leaf["was_first"]), len(leaves))
        return leaves[start:start + 3] if len(leaves) - start >= self.summary_settings.rollup_trigger else []

    def next_plan(self, day, *, rebuild=False):
        with connect(self.dsn) as c:
            policy = read_policy(c, self.space_id)
            revision = c.execute(
                "SELECT history_revision FROM memory_core.conversation_space_state WHERE bot_id=%s",
                (self.space_id,),
            ).fetchone()
            revision = revision["history_revision"] if revision else 0
            common = dict(
                day=day, source_revision=revision, policy_revision=policy["revision"]
            )
            names = dict(
                user_name=policy["participants"]["user"]["name"],
                assistant_name=policy["participants"]["assistant"]["name"],
            )
            batch = self._rollup_batch(c, day)
            if batch:
                chronological = sorted(batch, key=lambda s: datetime.fromisoformat(s["start_at"]))
                material = "\n\n".join(
                    text.SUMMARY_SOURCE.format(
                        number=i + 1,
                        start=s["start_at"],
                        end=s["end_at"],
                        content=s["content"],
                    )
                    for i, s in enumerate(chronological)
                )
                return dict(
                    common,
                    level=2,
                    source_ids=[s["id"] for s in batch],
                    source_digest=fingerprint(batch),
                    prompt=text.SUMMARY_ROLLUP_PROMPT.format(**names),
                    material=text.ROLLUP_MATERIAL.format(summaries=material),
                    start_at=chronological[0]["start_at"],
                    end_at=max((s["end_at"] for s in batch), key=datetime.fromisoformat),
                )
            turns = uncovered_turns(c, self.space_id, day)
            dialogue, _, groups = self._input(c, turns, policy)
            chars = sum(len(m["content"]) for g in groups for m in g["messages"])
            batch = summary_batch(turns, self.summary_settings, characters=chars)
            if not batch and rebuild:
                # A deleted turn can take a previously summarized batch below
                # the usual trigger. Keep the same recent-turn reserve.
                batch = turns[:-self.summary_settings.retain_turns]
            if not batch:
                return None
            dialogue, digest, _ = self._input(c, batch, policy)
            previous = c.execute(
                """SELECT content FROM memory_core.conversation_summaries WHERE bot_id=%s AND business_date=%s
                AND level=1 AND content_deleted_at IS NULL ORDER BY id DESC LIMIT 3""",
                (self.space_id, day),
            ).fetchall()
            return dict(
                common,
                level=1,
                source_ids=[t["id"] for t in batch],
                source_digest=digest,
                prompt=text.SUMMARY_PROMPT.format(**names),
                material=text.SUMMARY_MATERIAL.format(
                    previous_summaries="\n".join(f"- {s['content']}" for s in reversed(previous))
                    or "（今天还没有即时摘要）",
                    dialogue=dialogue,
                ),
                start_at=batch[0]["created_at"],
                end_at=batch[-1]["created_at"],
            )

    def save(self, plan, result, *, now=None):
        content, model = result.get("content"), result.get("model")
        if (
            not isinstance(content, str)
            or not content.strip()
            or not isinstance(model, str)
            or not 1 <= len(model) <= 256
        ):
            raise ValueError("摘要正文或型号无效")
        timestamp = format_timestamp(now or datetime.now(timezone.utc), self.settings)
        with connect(self.dsn) as c:
            policy = read_policy(c, self.space_id)
            self._lock(c)
            if policy["revision"] != plan["policy_revision"]:
                return None
            ids = plan["source_ids"]
            if plan["level"] == 1:
                turns = uncovered_turns(c, self.space_id, plan["day"])
                if [t["id"] for t in turns[: len(ids)]] != ids or len(turns) - len(
                    ids
                ) < self.summary_settings.retain_turns:
                    return None
                batch = turns[: len(ids)]
                _, digest, _ = self._input(c, batch, policy)
                if digest != plan["source_digest"]:
                    return None
                sources = [None, None, None]
            else:
                batch = self._rollup_batch(c, plan["day"])
                if [s["id"] for s in batch] != ids or fingerprint(batch) != plan["source_digest"]:
                    return None
                sources = ids
            row = c.execute(
                """INSERT INTO memory_core.conversation_summaries
                (bot_id,business_date,level,content,model,usage,source_revision,source_digest,start_at,end_at,created_at,
                 first_summary_id,second_summary_id,third_summary_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (
                    self.space_id,
                    plan["day"],
                    plan["level"],
                    content.strip(),
                    model,
                    Jsonb(result["usage"]) if result.get("usage") is not None else None,
                    plan["source_revision"],
                    plan["source_digest"],
                    plan["start_at"],
                    plan["end_at"],
                    timestamp,
                    *sources,
                ),
            ).fetchone()
            if plan["level"] == 1:
                for identifier in ids:
                    c.execute(
                        """INSERT INTO memory_core.conversation_summary_turns AS coverage
                            (summary_id,bot_id,business_date,turn_id) VALUES (%s,%s,%s,%s)
                        ON CONFLICT (bot_id,turn_id) DO UPDATE SET summary_id=EXCLUDED.summary_id
                        WHERE EXISTS (SELECT 1 FROM memory_core.conversation_summaries previous
                            WHERE previous.id=coverage.summary_id AND previous.bot_id=coverage.bot_id
                            AND previous.content_deleted_at IS NOT NULL)""",
                        (row["id"], self.space_id, plan["day"], identifier),
                    )
            else:
                c.execute(
                    "UPDATE memory_core.conversation_summaries SET merged_into_id=%s WHERE bot_id=%s AND id=ANY(%s)",
                    (row["id"], self.space_id, ids),
                )
            c.execute(
                "UPDATE memory_core.conversation_space_state SET history_revision=history_revision+1 WHERE bot_id=%s",
                (self.space_id,),
            )
            return row["id"]
