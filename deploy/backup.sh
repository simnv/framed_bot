#!/bin/sh
set -eu
umask 077
export PGHOST="${PGHOST:-db}" PGUSER="${PGUSER:-framed_backup}" PGDATABASE="${PGDATABASE:-framed}" PGCONNECT_TIMEOUT=10
BACKUP_DIR=${BACKUP_DIR:-/backups}
mkdir -p "$BACKUP_DIR"
pattern='framed-[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].dump'
tmp=
cleanup() { [ -z "$tmp" ] || rm -f -- "$tmp"; }
trap cleanup EXIT
trap 'exit 0' TERM INT

backup_once() {
    day=${BACKUP_DATE:-$(date -u +%F)}
    # Only strict, real UTC dates can enter the filename/retention namespace.
    [ "$(date -u -d "$day" +%F 2>/dev/null)" = "$day" ] || return 1
    final="$BACKUP_DIR/framed-$day.dump"
    [ ! -f "$final" ] || return 0
    tmp=$(mktemp "$BACKUP_DIR/.framed-$day.XXXXXX")
    PGPASSWORD=$(cat "${POSTGRES_PASSWORD_FILE:-/run/secrets/pg_backup_password}")
    export PGPASSWORD
    policy=$(psql -X -At --set ON_ERROR_STOP=1 -f "${ROLE_POLICY_FILE:-/role-policy.sql}" 2>/dev/null) || policy=failed
    if [ "$PGUSER" != framed_backup ] || [ "$policy" != "SET
t" ]; then
        unset PGPASSWORD
        cleanup
        tmp=
        printf '%s backup=failed\n' "$day" >&2
        return 1
    fi
    # A database can accept connections before the application creates its schema.
    schema=$(psql -Atqc "SELECT CASE WHEN to_regclass('public.user') IS NOT NULL AND to_regclass('public.framed_result') IS NOT NULL AND to_regclass('public.episode_result') IS NOT NULL THEN 'ready' ELSE 'waiting' END" 2>/dev/null) || schema=waiting
    if [ "$schema" = ready ] && pg_dump -Fc --no-owner --no-privileges -f "$tmp" 2>/dev/null && [ -s "$tmp" ] && pg_restore --list "$tmp" >/dev/null 2>&1; then
        mv -- "$tmp" "$final"
        tmp=
        # Prune only our exact successful-date filenames, after a validated atomic commit.
        find "$BACKUP_DIR" -maxdepth 1 -type f -name "$pattern" -printf '%f\n' |
            LC_ALL=C sort -r | sed '1,7d' | while IFS= read -r old; do
                case "$old" in framed-[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].dump) rm -- "$BACKUP_DIR/$old";; esac
            done
        printf '%s backup=ok\n' "$day"
        unset PGPASSWORD
        return 0
    fi
    unset PGPASSWORD
    cleanup
    tmp=
    printf '%s backup=failed\n' "$day" >&2
    return 1
}

case "${1:-loop}" in
    once) backup_once ;;
    health) [ -n "$(find "$BACKUP_DIR" -maxdepth 1 -type f -name "$pattern" -mmin -1800 -print -quit)" ] ;;
    loop) while :; do if backup_once; then sleep 60 & else sleep 300 & fi; wait "$!"; done ;;
    *) exit 2 ;;
esac
