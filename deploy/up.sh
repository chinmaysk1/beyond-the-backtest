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

# The worker: backtests and sweeps, plus the data refresher when idle.
# Recreated on every run so it always runs the image just built. Two cores: a
# sweep splits across two sandboxed processes -- see README.
docker volume inspect btb_pinecache >/dev/null 2>&1 || docker volume create btb_pinecache >/dev/null
docker rm -f btb-worker >/dev/null 2>&1 || true
docker run -d --name btb-worker --network "$NET" --restart unless-stopped \
  --cpus=2 --memory=4g \
  -v btb_pinecache:/var/cache/btb-pine \
  -e BTB_DB_PASSWORD="$BTB_DB_PASSWORD" -e TZ=UTC \
  btb-backend btb.engine worker --kinds backtest,sweep >/dev/null
echo "worker started (docker logs -f btb-worker)"

# The checker: answers Pine `check` jobs only, so they never wait behind a sweep.
docker rm -f btb-checker >/dev/null 2>&1 || true
docker run -d --name btb-checker --network "$NET" --restart unless-stopped \
  --cpus=0.5 --memory=512m \
  -e BTB_DB_PASSWORD="$BTB_DB_PASSWORD" -e TZ=UTC \
  btb-backend btb.engine worker --kinds check --no-refresh --poll 0.1 >/dev/null
echo "checker started"

# The web app and Caddy only when a domain is set: without one there is no
# certificate to get, and nothing public to serve.
if [ -n "${BTB_DOMAIN:-}" ]; then
  : "${BTB_SESSION_SECRET:?set BTB_SESSION_SECRET (32+ characters)}"
  docker build -q -t btb-web "$(dirname "$0")/../web" >/dev/null
  docker rm -f btb-web >/dev/null 2>&1 || true
  docker run -d --name btb-web --network "$NET" --restart unless-stopped \
    --cpus=0.75 --memory=1g \
    -e DATABASE_URL="postgresql://btb:${BTB_DB_PASSWORD}@btb-postgres:5432/btb" \
    -e SESSION_SECRET="$BTB_SESSION_SECRET" -e BTB_PUBLIC_URL="https://$BTB_DOMAIN" -e TZ=UTC \
    btb-web >/dev/null
  echo "web started"
  for v in btb_caddy_data btb_caddy_config; do docker volume inspect "$v" >/dev/null 2>&1 || docker volume create "$v" >/dev/null; done
  docker rm -f btb-caddy >/dev/null 2>&1 || true
  docker run -d --name btb-caddy --network "$NET" --restart unless-stopped \
    -p 80:80 -p 443:443 \
    -e BTB_DOMAIN="$BTB_DOMAIN" \
    -v "$(cd "$(dirname "$0")" && pwd)/Caddyfile:/etc/caddy/Caddyfile:ro" \
    -v btb_caddy_data:/data -v btb_caddy_config:/config \
    caddy:2.8-alpine >/dev/null
  echo "caddy started for https://$BTB_DOMAIN (docker logs -f btb-caddy)"
else
  echo "BTB_DOMAIN not set: web and caddy not started"
fi
