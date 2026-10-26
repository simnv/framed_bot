#!/bin/sh
# Authenticate both runtime logins and validate the persisted least-privilege policy.
set -eu
export PGHOST=127.0.0.1 PGDATABASE=framed PGCONNECT_TIMEOUT=3
for PGUSER in framed framed_backup; do
    export PGUSER
    case "$PGUSER" in framed) file=/run/secrets/pg_password;; *) file=/run/secrets/pg_backup_password;; esac
    PGPASSWORD=$(cat "$file") || exit 1
    export PGPASSWORD
    result=$(psql -X -At --set ON_ERROR_STOP=1 -f "${ROLE_POLICY_FILE:-/role-policy.sql}" 2>/dev/null) || exit 1
    [ "$result" = "SET
t" ] || exit 1
done
unset PGPASSWORD
