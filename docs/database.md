# Database

Caura stores everything in PostgreSQL. By default the stack runs a
bundled `postgres` service (`pgvector/pgvector:pg16`) with a Docker
volume — zero configuration, good for most on-prem installs.

You can instead point Caura at **your own PostgreSQL** (a managed
service like Amazon RDS, Google Cloud SQL, AlloyDB, Azure Database, or a
central self-managed cluster). This is useful when you already run
HA Postgres, want managed backups/PITR, or must keep data in a specific
instance for compliance.

## Requirements for an external instance

1. **PostgreSQL 14+** (the bundled image is pg16; 14 or newer is fine).
2. **The `pgvector` extension must be available.** Caura stores 1024-dim
   embeddings in `vector` columns and the migrations run
   `CREATE EXTENSION IF NOT EXISTS vector`. On managed Postgres this is
   supported but usually has to be enabled:
   - **RDS / Aurora**: `pgvector` is on the supported-extensions list; the
     `CREATE EXTENSION` call succeeds for a user with the `rds_superuser`
     role (or the extension pre-allow-listed).
   - **Cloud SQL / AlloyDB**: enable the `vector` (or `google_ml`) flag /
     extension, then `CREATE EXTENSION vector`.
   - **Azure Database for PostgreSQL**: allow-list `vector` in
     `azure.extensions`, then `CREATE EXTENSION vector`.
3. **A database + a user with `CREATE` privileges** on it — the first boot
   runs Alembic migrations (creates schemas `public` and `enterprise`,
   tables, indexes, and the extension). After the initial migration the
   user no longer needs DDL, but it's simplest to leave it.
4. Network reachability from the Caura host to the DB host/port.

## Configuration

Set these in `/opt/memclaw/.env` (or pass the matching `install.sh`
flags / `install.conf` keys). Blank `POSTGRES_HOST` = use the bundled
service.

```sh
POSTGRES_HOST=db.internal.acme.com   # external DB host (blank = bundled)
POSTGRES_PORT=5432                   # default 5432
POSTGRES_USER=memclaw                # default memclaw
POSTGRES_PASSWORD=<strong-password>  # the external DB user's password
POSTGRES_DB=memclaw                  # default memclaw
POSTGRES_REQUIRE_SSL=true            # true to require TLS to the DB
```

`install.sh` flags:

```sh
./install.sh --hostname caura.acme.com --license ./license.key \
  --postgres-host db.internal.acme.com \
  --postgres-port 5432 \
  --postgres-user memclaw \
  --postgres-db memclaw \
  --postgres-require-ssl
# (POSTGRES_PASSWORD is generated/prompted like any other secret, or set
#  it in .env before first boot)
```

`install.conf` keys: `postgres_host`, `postgres_port`, `postgres_user`,
`postgres_db`, `postgres_require_ssl`.

## The bundled `postgres` service in external mode

When `POSTGRES_HOST` points elsewhere, the bundled `postgres` service is
**not used** — all services connect to your external instance. The
service is still defined in `docker-compose.yml`, so by default it still
starts (idle, harmless). To stop running it entirely, comment out the
`postgres:` service block and the two `depends_on: postgres` entries
(under `platform-storage-api` and `core-storage-api`) in
`docker-compose.yml`.

## Migrations

Migrations run automatically on first boot (and on upgrades) from the
`platform-storage-api` and `core-storage-api` containers against whatever
`POSTGRES_HOST` resolves to. Nothing else is required — point at the DB,
bring the stack up, and the schema is created/upgraded in place.

> **Backups**: with an external managed instance, use that provider's
> backup/PITR. With the bundled service, back up the `pgdata` Docker
> volume (see `day2-ops.md`).
