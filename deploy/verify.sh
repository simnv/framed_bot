#!/bin/sh
# Approved post-deploy checks; creates/drops only a disposable restore-check database.
set -eu
. ./release.env
[ "$(hostname)" = "${FRAMED_NODE_HOSTNAME:?set target node hostname}" ]
PG=postgres:18.6-trixie
container() { docker ps --filter "label=com.docker.swarm.service.name=framed_$1" --format '{{.ID}}'; }
for service in bot db backup; do
    attempts=0
    while :; do
        id=$(container "$service")
        [ -z "$id" ] || [ "$(docker inspect --format '{{.State.Health.Status}}' "$id")" != healthy ] || break
        attempts=$((attempts + 1))
        [ "$attempts" -lt 60 ] || { printf '%s readiness=failed\n' "$service"; exit 1; }
        sleep 5
    done
    printf '%s health=healthy\n' "$service"
done
bot=$(container bot)
db=$(container db)
backup=$(container backup)
docker exec "$db" /bin/sh /db-health.sh
[ "$(docker exec "$db" stat -c '%u:%g:%a' /run/secrets/pg_password /run/secrets/pg_bootstrap_password /run/secrets/pg_backup_password)" = "999:999:400
999:999:400
999:999:400" ]
[ "$(docker exec "$backup" stat -c '%a' /run/secrets/pg_backup_password)" = 400 ]
for runtime in "$bot" "$backup"; do
    docker exec "$runtime" /bin/sh -c 'test ! -e /run/secrets/pg_bootstrap_password'
done
[ "$(docker inspect --format '{{.Image}}' "$bot")" = "$(cat image.id)" ]
docker exec "$bot" python healthcheck.py
docker exec "$bot" python -c 'import os; from pathlib import Path; p=Path("/data"); assert os.getuid() == 10001; t=p/".write-check"; t.write_text("ok"); t.unlink()'
latest=$(docker run --rm -v framed_backups:/backups:ro --entrypoint /bin/sh "$PG" -c 'find /backups -maxdepth 1 -name "framed-[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].dump" | sort | tail -1')
[ -n "$latest" ]
checkdb=framed_restore_check_$(od -An -N12 -tx1 /dev/urandom | tr -d ' \n')
# Bootstrap handles only the disposable database lifecycle. Runtime restore uses framed.
work=$(mktemp -d)
created=false
cleanup() {
    if [ "$created" = true ]; then
        docker exec "$db" dropdb -U framed_bootstrap --if-exists "$checkdb" >/dev/null 2>&1 || true
    fi
    rm -rf -- "$work"
}
trap cleanup EXIT
trap 'exit 1' TERM INT
docker exec "$db" createdb -U framed_bootstrap "$checkdb"
created=true
# Restore directly from the backup volume, with independent producer/consumer exit statuses.
docker exec -i "$db" psql -X -v ON_ERROR_STOP=1 -v "checkdb=$checkdb" -U framed_bootstrap -d "$checkdb" < context/deploy/restore-prepare.sql >/dev/null
mkfifo "$work/archive"
docker run --rm -v framed_backups:/backups:ro --entrypoint /bin/cat "$PG" "$latest" >"$work/archive" &
producer=$!
restore_status=0
docker exec -i "$db" /bin/sh -c 'PGPASSWORD=$(cat /run/secrets/pg_password); export PGPASSWORD; exec pg_restore --exit-on-error --no-owner --no-privileges -h 127.0.0.1 -U framed -d "$1"' sh "$checkdb" <"$work/archive" || restore_status=$?
wait "$producer"
[ "$restore_status" -eq 0 ]
[ "$(docker exec "$db" psql -U framed -d "$checkdb" -Atc "SELECT count(*) = 3 FROM information_schema.columns WHERE data_type='bigint' AND ((table_name='user' AND column_name='id') OR (table_name IN ('framed_result','episode_result') AND column_name='user_id'))")" = t ]
printf 'image_id=verified authenticated_db=ok persistence_permissions=ok restore_schema=ok\n'
docker service ls --format '{{.ID}} {{.Name}} {{.Image}} {{.Replicas}}'
