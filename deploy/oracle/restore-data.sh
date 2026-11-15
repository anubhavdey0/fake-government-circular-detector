#!/usr/bin/env bash
# Load an index archive made by scripts/pack_data.py into the compose "data" volume.
#
#   bash deploy/oracle/restore-data.sh ~/circverify-data.tar
set -euo pipefail

ARCHIVE="$(realpath "${1:?usage: restore-data.sh circverify-data.tar}")"
DIR="${DIR:-/opt/circverify}"
cd "$DIR"

sudo docker compose stop web watcher 2>/dev/null || true
# Unpack with the app's own image and volume, then hand the files to the app's user.
sudo docker compose run --rm --no-deps --user root -v "$ARCHIVE:/in.tar:ro" web \
  sh -c 'tar -xf /in.tar -C /data && chown -R circverify /data && ls -la /data'
echo "Index loaded. Start with: sudo docker compose --profile https up -d"
