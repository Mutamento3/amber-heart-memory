ALTER TABLE memory_web.space_access_keys ALTER COLUMN ciphertext DROP NOT NULL;
UPDATE memory_web.space_access_keys SET ciphertext=NULL WHERE revoked_at IS NOT NULL;
ALTER TABLE memory_web.space_access_keys ADD CONSTRAINT access_key_live_ciphertext
    CHECK ((revoked_at IS NULL)=(ciphertext IS NOT NULL));
