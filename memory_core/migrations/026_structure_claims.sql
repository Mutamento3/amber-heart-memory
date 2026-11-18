ALTER TABLE memory_core.pending_memories
    ADD COLUMN structure_claim_token uuid,
    ADD COLUMN structure_claimed_at text,
    ADD COLUMN structure_claim_until text,
    ADD CONSTRAINT pending_memories_structure_claim_complete CHECK (
        num_nonnulls(structure_claim_token, structure_claimed_at, structure_claim_until) IN (0, 3)
    );
