#!/bin/sh
# Read-only checks; requires an explicit target hostname argument.
set -eu
[ "$(hostname)" = "${1:?set target node hostname}" ]
[ "$(docker info --format '{{.Swarm.ControlAvailable}}')" = true ]
[ ! -e /opt/framed_bot ]
[ -z "$(docker service ls --filter name=framed --format '{{.Name}}')" ]
[ -z "$(docker network ls --filter name=framed --format '{{.Name}}')" ]
[ -z "$(docker volume ls --filter name=framed --format '{{.Name}}')" ]
[ -z "$(docker secret ls --filter name=framed --format '{{.Name}}')" ]
[ -z "$(docker config ls --filter name=framed --format '{{.Name}}')" ]
printf 'preflight=ok\n'
# Narrow identities for comparison, no environment/secret inspection.
docker service ls --format '{{.ID}} {{.Name}} {{.Image}} {{.Replicas}}'
