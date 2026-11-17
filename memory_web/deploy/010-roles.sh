#!/bin/sh
set -eu
# Runs only on an empty database volume; passwords are psql-quoted variables.
psql --username postgres --dbname amber_heart --set ON_ERROR_STOP=1 \
  --set owner_password="$MEMORY_DB_OWNER_PASSWORD" \
  --set app_password="$MEMORY_DB_APP_PASSWORD" <<'SQL'
CREATE ROLE memory_owner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD :'owner_password';
CREATE ROLE memory_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD :'app_password';
ALTER DATABASE amber_heart OWNER TO memory_owner;
REVOKE ALL ON DATABASE amber_heart FROM PUBLIC;
GRANT CONNECT ON DATABASE amber_heart TO memory_app;
CREATE EXTENSION vector WITH SCHEMA public;
SQL
