#!/usr/bin/env bash
# Bring the stack up on a host without the docker compose plugin.
#
# `docker-compose.yml` beside this file is the source of truth for what should
# run; this script does the same thing with plain `docker run`, because the
# Oracle box has Docker 29 from the Ubuntu repo and no compose plugin. Installing
# it needs sudo on a machine shared with three other services, so this exists
# rather than changing something on a host that is working.
#
# Idempotent: safe to re-run. It never drops the data volume.
#
#   BTB_DB_PASSWORD=... deploy/up.sh
#
set -euo pipefail

: "${BTB_DB_PASSWORD:?set BTB_DB_PASSWORD (see backend/.env.local)}"

NET=btb
VOLUME=btb_pgdata
PG=btb-postgres

docker network inspect "$NET"   >/dev/null 2>&1 || docker network create "$NET"
docker volume  inspect "$VOLUME" >/dev/null 2>&1 || docker volume create "$VOLUME"

if [ -z "$(docker ps -q -f name="^${PG}$")" ]; then
  # Recreate the container if it is stopped, but never the volume: the database
  # is the asset, and a `docker run` that quietly starts on an empty volume is
  # indistinguishable from one that worked.
  docker rm -f "$PG" >/dev/null 2>&1 || true
  docker run -d --name "$PG" --network "$NET" --restart unless-stopped \
    -e POSTGRES_USER=btb \
    -e POSTGRES_PASSWORD="$BTB_DB_PASSWORD" \
    -e POSTGRES_DB=btb \
    -e TZ=UTC -e PGTZ=UTC \
    -p 127.0.0.1:5432:5432 \
    -v "$VOLUME":/var/lib/postgresql/data \
    --shm-size=256m \
    postgres:16-alpine
  echo "started $PG"
else
  docker network connect "$NET" "$PG" 2>/dev/null || true
  echo "$PG already running"
fi

until docker exec "$PG" pg_isready -U btb -d btb >/dev/null 2>&1; do sleep 1; done
echo "postgres ready"

if [ -d "$(dirname "$0")/../backend" ]; then
  docker build -q -t btb-backend "$(dirname "$0")/../backend" >/dev/null
  echo "backend image built"
fi

docker run --rm --network "$NET" -e BTB_DB_PASSWORD="$BTB_DB_PASSWORD" \
  btb-backend btb.data init
