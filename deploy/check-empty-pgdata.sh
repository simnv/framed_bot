#!/bin/sh
# Read-only first-deployment guard. A normal service restart does not run this.
set -eu
[ -d "${1:-/check}" ] && [ -z "$(find "${1:-/check}" -mindepth 1 -print -quit)" ] || {
    printf 'Refusing nonempty database volume; explicit operator recovery required\n' >&2
    exit 1
}
