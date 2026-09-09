# Install guide (connected)

This guide walks you through installing Caura Enterprise on a
customer-managed VM with outbound internet access. For air-gapped
installs see `install-airgap.md`.

## Prerequisites

- Ubuntu 22.04 or equivalent Linux distribution.
- **Docker ≥ 24** and **docker compose v2** (both come bundled with
  recent Docker Desktop / Docker CE).
- **8 GB RAM** (16 GB recommended for >50 concurrent users).
- **50 GB disk** free for Postgres + Redis + RabbitMQ.
- A DNS record pointing to the VM (e.g. `caura.acme.com → 10.0.0.42`).
- A TLS certificate for that hostname (`cert.pem` + `key.pem`), or use the
  default self-signed cert / Let's Encrypt — see `TLS.md`.
- A **license key** issued by Caura (`.key` file).
- An **admin email at a real, deliverable domain** — addresses at reserved
  TLDs (`.local`, `.test`, `.example`, `localhost`) are rejected by email
  validation and admin creation will fail (see `troubleshooting.md`). Use
  e.g. `admin@yourcompany.com`.
- An **embedding provider**: by default the stack expects an LLM/embedding
  API key (OpenAI / Anthropic / Gemini) set in `.env`. Fully self-contained
  local embeddings are a separate option — see "Embedding provider" below.

> **Optional — external PostgreSQL.** By default the stack runs a bundled
> Postgres. To use a managed/external instance (RDS, Cloud SQL, AlloyDB, a
> central cluster), see `database.md` for the env vars and the required
> `pgvector` extension.

## Version pinning

`CAURA_VERSION` in `.env` controls which image tag every service runs.
The installer pins it to a specific release (e.g. `v2.8.4`) by default —
keep it pinned to a release tag rather than `latest` so the deployment is
reproducible and every service runs the same version. Bump it deliberately
when upgrading (see `upgrade.md`).

## Embedding provider

Caura needs an embedding backend for semantic recall:

- **Connected (recommended):** set `EMBEDDING_PROVIDER=openai` (or
  anthropic / gemini) and provide the matching API key in `.env`. Memory
  *writes* succeed without it (embedding is deferred), but *recall* /
  semantic search requires a reachable provider, or it returns
  `503 Embedding service unavailable`.
- **Fully local / air-gapped:** uses a bundled embedder image with the
  model weights baked in (no external calls). See `install-airgap.md` for
  current availability and how to enable it.

## Option A — zero-config one-liner

```bash
curl -sL https://onprem.caura.ai/install.sh | bash
```

What it does:

1. Runs preflight checks (Docker, RAM, disk).
2. Generates a secure `JWT_SECRET`, `POSTGRES_PASSWORD`, and admin API key.
3. Writes `.env` to `/opt/memclaw/`.
4. Pulls pinned images from `ghcr.io/caura-ai`.
5. Starts the stack with `docker compose up -d`.
6. Prints a URL like `https://<detected-host>/setup`.

Visit the URL in a browser, upload your license, create the first admin,
and you're done.

## Option B — silent install (Ansible / Terraform / runbook)

Provide every required value upfront via a config file:

```bash
./install.sh --config /etc/memclaw/install.conf --non-interactive
```

See `install.conf.example` for the full template. The installer writes a
machine-readable `install-result.json` for your automation to consume.

## Creating the first admin

There are two ways, both hitting the same first-run bootstrap:

- **Web wizard (interactive):** after a zero-config install (or
  `--skip-admin`), open `/setup` in a browser, upload the license, and
  create the first admin there.
- **API call (automated):** passing `--admin-email` + `--admin-password`
  to `install.sh` makes it `POST /api/setup/admin` for you. You can also
  call it directly:

  ```bash
  curl -sk -X POST https://$PUBLIC_HOSTNAME/api/setup/admin \
    -H 'Content-Type: application/json' \
    -d '{"email":"admin@yourcompany.com","password":"<strong>","org_name":"yourorg"}'
  # → {"ok":true,"org_id":"…","tenant_id":"…","api_key":"mc_…"}
  ```

  The returned `api_key` is shown **once** — store it. Use a real email
  domain (reserved TLDs are rejected — see Prerequisites).

This endpoint is a **one-time bootstrap**: once an admin exists, the whole
`/setup/*` surface returns `404`, so it cannot be re-run. The trust anchor
is possession of the license file plus being first to call it.

## Verifying the install

```bash
curl -sk https://$PUBLIC_HOSTNAME/api/version
curl -sk https://$PUBLIC_HOSTNAME/api/license/status \
  -H "Authorization: Bearer $ADMIN_JWT"
```

The license status should return `{"configured": true, "severity": "ok", ...}`.
Log in to the dashboard; a yellow or red banner appears at the top when
the license is approaching expiry.

## Upgrading

See `upgrade.md`.

## Troubleshooting

See `troubleshooting.md`.
