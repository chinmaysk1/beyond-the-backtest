# deploy

What runs on the server, recorded as files rather than as shell history.

## Host

Oracle Cloud, Ampere ARM (`aarch64`), 2 cores, 11 GB RAM, 49 GB disk.
**Not EC2** — backups go through Oracle block volume backup, not EBS snapshots.

Already running, and untouched by anything here:

| Service | Port |
|---|---|
| open-webui | 3001 |
| n8n | 5678 |
| quantlab-dash | 8088 |

Ports 80 and 443 are free, which is where Caddy will go in Weeks 9–11.

## Bringing it up

```bash
BTB_DB_PASSWORD=... deploy/up.sh
```

`docker-compose.yml` is the source of truth for what *should* run. The box has
Docker 29 without the compose plugin, so `up.sh` reproduces it with plain
`docker run`. Installing the plugin needs sudo on a machine shared with three
other services; that can happen later, and the compose file is already written
for when it does.

## Connecting from a laptop

Postgres is bound to `127.0.0.1` on the host and joined to the `btb` Docker
network. It is never published publicly. To reach it for development:

```bash
ssh -L 55432:127.0.0.1:5432 ubuntu@<host>
```

`backend/btb/db.py` defaults to port 55432 for exactly this reason. Inside the
Docker network, containers use `btb-postgres:5432` instead.

The tunnel drops on idle; re-open it and carry on. Nothing running on the server
depends on it — the backfill and the worker talk to Postgres over the Docker
network, not through the tunnel.

## Running a backfill on the server

Long ingests belong on the box, not on a laptop that gets closed:

```bash
docker run -d --name btb-backfill --network btb --cpus=1 --memory=2g \
  -e BTB_DB_PASSWORD=... btb-backend btb.data build
docker logs -f btb-backfill
```

One core, deliberately. Both cores are shared with open-webui and n8n; pinning
both makes those visibly laggy, and the sweep finishing in 30 seconds instead of
15 is not worth it.

Ingestion is resumable — it reads the newest stored bar and asks only for what
comes after — so a killed container costs nothing but the series in flight.

## Secrets

The database password lives in `backend/.env.local` (gitignored) and on the box
in `~/btb/.pgpass`. It is passed to containers through the environment and is
never baked into an image.

## Firewall note

Opening a port on Oracle Cloud takes **two** changes: the OCI security list in
the console, *and* iptables on the host. Ubuntu images there ship restrictive
rules that silently drop traffic after the console change looks correct.
