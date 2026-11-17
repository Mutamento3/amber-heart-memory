CREATE SCHEMA memory_web;
REVOKE ALL ON SCHEMA memory_web FROM PUBLIC;
CREATE TABLE memory_web.admin_sessions (
    digest text PRIMARY KEY CHECK (digest ~ '^[0-9a-f]{64}$'),
    csrf text NOT NULL,
    password_version text NOT NULL,
    expires_at text NOT NULL,
    created_at text NOT NULL
);
CREATE INDEX admin_sessions_expiry ON memory_web.admin_sessions(expires_at);
CREATE TABLE memory_web.admin_login_attempts (
    bucket text PRIMARY KEY,
    count integer NOT NULL CHECK (count>0),
    expires_at text NOT NULL
);
REVOKE ALL ON ALL TABLES IN SCHEMA memory_web FROM PUBLIC;
