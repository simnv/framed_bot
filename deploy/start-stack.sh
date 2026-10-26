#!/bin/sh
# This is the approved mutation step; only use after native build and secret upload.
set -eu
. ./release.env
[ "$(hostname)" = "${FRAMED_NODE_HOSTNAME:?set target node hostname}" ]
export FRAMED_RELEASE FRAMED_IMAGE ADMIN_USER_ID FRAMED_NODE_HOSTNAME
[ "$(docker image inspect --format '{{.Id}}' "$FRAMED_IMAGE")" = "$(cat image.id)" ]
[ "$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$FRAMED_IMAGE")" = "$FRAMED_PLATFORM" ]
[ -z "$(docker service ls --filter name=framed --format '{{.Name}}')" ]
for name in framed_pgdata framed_botdata framed_backups; do
    if docker volume inspect "$name" >/dev/null 2>&1; then
        [ "$(docker volume inspect --format '{{ index .Labels "framed.owner" }} {{ index .Labels "framed.release" }}' "$name")" = "framed $FRAMED_RELEASE" ] || {
            printf 'Refusing conflicting volume: %s\n' "$name" >&2
            exit 1
        }
    fi
done
for name in framed_bot_token_v1 framed_db_url_v1 framed_pg_password_v1 framed_pg_bootstrap_password_v1 framed_pg_backup_password_v1; do
    docker secret inspect "$name" >/dev/null
done
docker pull postgres:18.6-trixie
docker stack config -c context/deploy/stack.yml >/dev/null
if docker volume inspect framed_pgdata >/dev/null 2>&1; then
    docker run --rm -i --mount type=volume,src=framed_pgdata,dst=/check,readonly --entrypoint /bin/sh postgres:18.6-trixie -s < context/deploy/check-empty-pgdata.sh
fi
for name in framed_pgdata framed_botdata framed_backups; do
    docker volume inspect "$name" >/dev/null 2>&1 || docker volume create --label framed.owner=framed --label "framed.release=$FRAMED_RELEASE" "$name" >/dev/null
done
docker stack deploy --resolve-image never -c context/deploy/stack.yml framed
printf 'stack_submitted=framed\n'
