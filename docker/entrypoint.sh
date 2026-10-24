#!/bin/sh
# Drop root privileges to PUID:PGID (Unraid default 99:100) after making /config writable.
# If the container is started with --user, run as that user without touching ownership.
set -eu

umask "${UMASK:-022}"

if [ "$(id -u)" = "0" ]; then
  PUID="${PUID:-99}"
  PGID="${PGID:-100}"
  mkdir -p "${MEDIAHUB_CONFIG_DIR:-/config}"
  chown -R "$PUID:$PGID" "${MEDIAHUB_CONFIG_DIR:-/config}"
  # Changing to a non-root uid clears effective/permitted capabilities.
  exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups -- "$@"
fi

exec "$@"
