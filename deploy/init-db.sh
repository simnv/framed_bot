#!/bin/sh
# Official-entrypoint fresh-cluster initialization only. Never repair existing roles.
set -eu
fail() { printf 'Database role initialization failed; details suppressed\n' >&2; exit 1; }
APP_PASSWORD=$(cat /run/secrets/pg_password) || fail
BACKUP_PASSWORD=$(cat /run/secrets/pg_backup_password) || fail
case "$APP_PASSWORD:$BACKUP_PASSWORD" in *[!0-9a-f:]*|:*) fail;; esac
[ "${#APP_PASSWORD}" -ge 32 ] && [ "${#BACKUP_PASSWORD}" -ge 32 ] && [ "$APP_PASSWORD" != "$BACKUP_PASSWORD" ] || fail
export APP_PASSWORD BACKUP_PASSWORD
# Disable statement logging before expanding password literals, including error logging.
if ! psql -X --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set ON_ERROR_STOP=1 >/dev/null 2>&1 <<'SQL'
SET log_statement = 'none';
SET log_min_error_statement = 'panic';
\getenv app_password APP_PASSWORD
\getenv backup_password BACKUP_PASSWORD
BEGIN;
CREATE ROLE framed LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD :'app_password';
CREATE ROLE framed_backup LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD :'backup_password';
REVOKE ALL ON DATABASE framed FROM PUBLIC;
GRANT CONNECT ON DATABASE framed TO framed, framed_backup;
REVOKE CONNECT, TEMPORARY ON DATABASE postgres, template1 FROM PUBLIC;
ALTER SCHEMA public OWNER TO framed;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO framed_backup;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO framed_backup;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO framed_backup;
ALTER DEFAULT PRIVILEGES FOR ROLE framed IN SCHEMA public GRANT SELECT ON TABLES TO framed_backup;
ALTER DEFAULT PRIVILEGES FOR ROLE framed IN SCHEMA public GRANT SELECT ON SEQUENCES TO framed_backup;
ALTER DEFAULT PRIVILEGES FOR ROLE framed REVOKE EXECUTE ON ROUTINES FROM PUBLIC;
COMMIT;
SQL
then fail; fi
unset APP_PASSWORD BACKUP_PASSWORD
