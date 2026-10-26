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

Ports 80 and 443 are free; Caddy takes them when the site goes public.

What this project runs there:

| Container | Does | Limits |
|---|---|---|
| `btb-postgres` | the database, on `127.0.0.1` and the `btb` network only | |
| `btb-worker` | backtests and sweeps (Pine through PineForge), data refresh when idle | 2 cores, 4 GB |
| `btb-checker` | answers Pine `check` jobs only, so a sweep never delays a check | 0.5 core, 512 MB |
| `btb-web` | the Next.js app and the MCP server; reachable only through Caddy | 0.75 core, 1 GB |
| `btb-caddy` | TLS for `BTB_DOMAIN`, the only public ports | |

The worker has both cores because a Pine sweep splits across two sandboxed
processes; open-webui can lag while a long 15m sweep runs, accepted for sweeps
that finish in minutes rather than ten.

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

## Deploying the backend

The box has no git checkout. `~/btb/` holds a copy of `backend/` and `deploy/`,
shipped from a laptop and rebuilt in place:

```bash
tar --exclude=__pycache__ -czf - backend/Dockerfile backend/requirements.txt \
    backend/universe.json backend/btb deploy \
  | ssh ubuntu@<host> 'cd ~/btb && tar -xzf - && \
      BTB_DB_PASSWORD="$(cat ~/btb/.pgpass)" bash deploy/up.sh'
docker logs -f btb-worker          # on the box
```

`up.sh` rebuilds the image, re-applies the schema (idempotent), and recreates
`btb-worker` and `btb-checker`. With `BTB_DOMAIN` and `BTB_SESSION_SECRET` set
it also builds and starts `btb-web` and `btb-caddy` (ship `web/` too, minus
`node_modules` and `.next`):

```bash
BTB_DB_PASSWORD=... BTB_DOMAIN=btb.example.com BTB_SESSION_SECRET=... bash deploy/up.sh
```

Before that: point the domain's A record at the box, and open TCP 80 and 443 in
the OCI security list and in iptables (see the firewall note). Caddy gets the
certificate on its first start once both are true.

The first sweep of a new script compiles it (4-7 s on this box); compiled
strategies are kept in the `btb_pinecache` volume, so the same script never
compiles twice, across restarts too. The worker loads the strategy library into the database on start,
claims `backtest` and `sweep` jobs, and between jobs refreshes any series with a
newly closed bar. A refresh gives way as soon as a job is queued, so a click in
the web app never waits behind a data catch-up.

The first job after the worker starts compiles the fill loop with numba, about
8 s on this box. The compiled code is cached in the container, so every job
after it starts immediately; recreating the container pays the 8 s once more.

`deploy/*.sh` must have LF line endings (`.gitattributes` enforces it). With
CRLF, bash reads `set -o pipefail\r` and refuses to run.

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

No file in this repo holds a database URL with credentials, not even a
placeholder one: `up.sh` assembles the web app's `DATABASE_URL` from
`BTB_DB_PASSWORD` at run time, and `docker-compose.yml` takes it whole from
`BTB_DATABASE_URL` (`postgresql://btb` + `:` + the URL-encoded password +
`@btb-postgres:5432/btb`). Secret scanners flag the `user:password@host` shape
even when the password is a `${...}` placeholder.

## Firewall note

Opening a port on Oracle Cloud takes **two** changes: the OCI security list in
the console, *and* iptables on the host. Ubuntu images there ship restrictive
rules that silently drop traffic after the console change looks correct.
