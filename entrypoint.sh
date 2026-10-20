#!/bin/sh
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

case "$1" in
  hashpw)
    exec python -m app.hashpw
    ;;
  serve) ;;
  *)
    exec "$@"
    ;;
esac

# Run as the host user so files in the library belong to them, not root.
if ! getent group "$PGID" >/dev/null; then groupadd -g "$PGID" app; fi
if ! getent passwd "$PUID" >/dev/null; then
  useradd -u "$PUID" -g "$PGID" -d /data/home -M -s /usr/sbin/nologin app
fi

mkdir -p /config/cookies /data/home /data/cache /data/tmp /data/thumbs
chown "$PUID:$PGID" /config /config/cookies /data /data/home /data/cache /data/tmp /data/thumbs
chmod 700 /config/cookies
# Only the library root itself is checked; its contents are never chowned.
[ -d /library ] || { echo "ERROR: /library is not mounted (set LIBRARY_PATH in .env)"; exit 1; }

# The engines venv must be writable by the app user for "Update engines".
if [ "$(stat -c %u /opt/engines)" != "$PUID" ]; then
  echo "Preparing engines directory for uid $PUID..."
  chown -R "$PUID:$PGID" /opt/engines
fi

umask "${UMASK:-022}"

if [ "${AUTO_UPDATE_ENGINES:-true}" = "true" ]; then
  echo "Updating yt-dlp and gallery-dl..."
  if ! gosu "$PUID:$PGID" timeout 300 /opt/engines/bin/pip install --upgrade --quiet -r /tmp/engines.txt; then
    echo "WARNING: engine update failed, starting with the installed versions"
  fi
fi
echo "yt-dlp $(/opt/engines/bin/yt-dlp --version 2>/dev/null || echo '?'), gallery-dl $(/opt/engines/bin/gallery-dl --version 2>/dev/null || echo '?')"

exec gosu "$PUID:$PGID" uvicorn app.main:app \
  --host 0.0.0.0 --port 8790 \
  --proxy-headers --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-*}" \
  --no-server-header --log-level info
