#!/bin/sh
# version 1.2.0
# Prepare application data and generated-output mounts, then run as the host UID/GID.
set -eu

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
APP_DATA="${APP_DATA:-/app/data}"
OUTPUT_ROOT="${OUTPUT_ROOT:-/media/output}"

case "${PUID}:${PGID}" in
  *[!0-9:]*|:*|*:)
    echo "ERROR: PUID and PGID must be numeric values (for example 1000:1000)." >&2
    exit 64
    ;;
esac

if [ "$(id -u)" -eq 0 ]; then
  echo "ClipQueue: preparing writable folders for UID:GID ${PUID}:${PGID}."

  for directory in "$APP_DATA" "$OUTPUT_ROOT"; do
    if ! mkdir -p "$directory"; then
      echo "ERROR: Cannot create writable folder: $directory" >&2
      exit 73
    fi

    # This only affects application data and generated output. The input mount is
    # deliberately never ownership-modified; it remains fully owned by the host user.
    if ! chown -R "${PUID}:${PGID}" "$directory"; then
      echo "ERROR: Cannot set ownership of $directory to ${PUID}:${PGID}." >&2
      echo "Check that the host mount is local and allows Docker to write to it." >&2
      exit 73
    fi
  done

  exec gosu "${PUID}:${PGID}" "$@"
fi

exec "$@"
