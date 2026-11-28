-- Configuration revisions advance with unfinished work. Repair existing
-- rows while preserving the original review snapshots and waiting clocks.
UPDATE memory_core.pending_memories AS draft
SET policy_revision = policy.revision, version = draft.version + 1
FROM memory_core.space_policies AS policy
WHERE draft.bot_id = policy.bot_id
  AND draft.policy_revision <> policy.revision
  AND draft.status IN ('pending', 'structure_pending', 'structure_failed');

UPDATE memory_core.material_extractions AS extraction
SET policy_revision = policy.revision, version = extraction.version + 1
FROM memory_core.space_policies AS policy
WHERE extraction.bot_id = policy.bot_id
  AND extraction.policy_revision <> policy.revision
  AND extraction.status IN ('pending', 'selected', 'failed');

UPDATE memory_core.topic_summary_batches AS batch
SET policy_revision = policy.revision, version = batch.version + 1
FROM memory_core.space_policies AS policy
WHERE batch.bot_id = policy.bot_id
  AND batch.policy_revision <> policy.revision
  AND batch.status IN ('pending', 'generating', 'failed');
