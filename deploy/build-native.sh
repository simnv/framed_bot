#!/bin/sh
# Run from the uploaded immutable release directory on the production host. No polling here.
set -eu
. ./release.env
[ "$(hostname)" = "${FRAMED_NODE_HOSTNAME:?set target node hostname}" ]
case "${FRAMED_PLATFORM:?set build platform}" in
    linux/amd64) [ "$(uname -m)" = x86_64 ] ;;
    linux/arm64) [ "$(uname -m)" = aarch64 ] ;;
    *) exit 1 ;;
esac
sha256sum -c source.sha256
[ ! -e context ]
[ -z "$(docker image ls -q "$FRAMED_IMAGE")" ]
mkdir context
tar -xzf source.tar.gz -C context
(cd context && sha256sum -c ../files.sha256)
docker buildx build --platform "$FRAMED_PLATFORM" --load --provenance=false --tag "$FRAMED_IMAGE" context
[ "$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$FRAMED_IMAGE")" = "$FRAMED_PLATFORM" ]
docker image inspect --format '{{.Id}}' "$FRAMED_IMAGE" > image.id
chmod 600 image.id
printf 'native_build=ok image_id='
cat image.id
