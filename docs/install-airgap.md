# Install guide — air-gapped

For customer VMs with **no outbound internet**. The tarball bundles every
image needed (Caura services + upstream bases: Postgres/pgvector, Redis,
RabbitMQ) so `docker compose` never needs to pull.

> **Local embeddings (`--embedding-provider local`).** A true air-gap
> install needs the bundled embedder image (`memclaw-core-api-embedder`,
> model weights baked in, ~1.5 GB) — it swaps in via
> `docker-compose.embedder.yml` and the installer engages it automatically
> when `EMBEDDING_PROVIDER=local` and no LLM key is set. **Confirm the
> embedder image for your target version is present in the release
> tarball before installing** (`docker images | grep core-api-embedder`,
> tag must match `CAURA_VERSION`). If it's absent, either obtain a
> tarball that includes it from Caura, or run connected with an external
> embedding provider (OpenAI / Anthropic / Gemini). Without an embedder
> image, memory writes still work but semantic recall returns
> `503 Embedding service unavailable`.

## Prerequisites

- Ubuntu 22.04 or equivalent, **Docker ≥ 24**, **docker compose v2**.
- **8 GB RAM**, **50 GB disk**.
- A DNS record for the customer hostname pointing to the VM.
- A TLS cert for that hostname (`cert.pem` + `key.pem`).
- The Caura-issued `license.key` file.
- The release tarball:
  - `memclaw-onprem-<version>.tar.gz` (images, ≈2 GB compressed)
  - The bundle repo contents (`docker-compose.yml`, `install.sh`, etc. —
    text-only, KB-sized)

Transfer both to the VM however you prefer (SCP over an internal bastion,
USB, physical media).

## 1. Load the images

```bash
./airgap-load.sh /path/to/memclaw-onprem-<version>.tar.gz
# → Loading images from memclaw-onprem-<version>.tar.gz
# → Loaded images:
#     memclaw-onprem/platform-auth:v1.0.0
#     memclaw-onprem/platform-admin:v1.0.0
#     ...
#     pgvector/pgvector:pg16
#     redis:7-alpine
#     rabbitmq:3-management-alpine
```

The script auto-detects `memclaw-onprem-*.tar.gz` in the current directory
if you don't pass a path.

## 2. Run the installer in offline mode

```bash
./install.sh \
  --non-interactive \
  --offline \
  --hostname caura.acme.com \
  --admin-email admin@acme.com \
  --admin-password-file /run/secrets/admin_pw \
  --license /path/to/license.key \
  --embedding-provider local \
  --email-provider log \
  --version v1.0.0
```

The `--offline` flag:
- Skips `docker compose pull` (which would 401 without ghcr credentials)
- Verifies the upstream base images (`pgvector/pgvector:pg16`, `redis:7-alpine`,
  `rabbitmq:3-management-alpine`) are present locally — fails fast if not
- Uses `docker-compose.airgap.yml` overlay so service images resolve to
  `memclaw-onprem/*:<version>` (the locally-loaded tags)

If you're running as a non-root user, `install.sh` auto-re-executes via
`sudo` — add your user to the `docker` group first to avoid the prompt:

```bash
sudo usermod -aG docker $(whoami) && newgrp docker
```

## 3. Bootstrap wizard

Two options, both do the same thing:

**A. Pass all values to install.sh (as above)** — fully unattended.

**B. Pass `--skip-admin` and finish in the browser**:
```bash
./install.sh --non-interactive --offline --hostname ... --license ... --skip-admin
# → Visit https://caura.acme.com/setup
```

The web wizard uploads the license, creates the first admin, and prints
the API key exactly once. It then disappears — re-visiting `/setup`
returns 404 from then on (enforced server-side by the first-run gate in
`platform_auth_api.routers.setup._admin_exists()`).

## 4. Verify

```bash
# Health via the gateway (port 80)
curl -sf http://caura.acme.com/healthz
# → ok

# License status — behind JWT auth, use the admin account you just created
JWT=$(curl -s -X POST http://caura.acme.com/api/auth/user/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@acme.com","password":"..."}' \
  | jq -r .access_token)

curl -s http://caura.acme.com/api/license/status \
  -H "Authorization: Bearer $JWT" | jq '.severity, .days_remaining'
# → "ok"
# → 364
```

All 11 containers should be `running`:

```bash
docker compose -f /opt/memclaw/docker-compose.yml \
  -f /opt/memclaw/docker-compose.airgap.yml ps
```

## 5. Phone-home: off by default in air-gap

Air-gap licenses are issued with `phone_home_enabled=false`. The on-prem
stack never attempts to reach `ops.caura.ai` and a `tcpdump` from the VM
during a full smoke run shows zero outbound traffic outside RFC1918.

If your licence needs to be re-issued (expiry approaching, new seat
count), Caura mints a replacement in their ops portal, you transfer the
new `.key` file to the VM and drop it in:

```bash
sudo cp new-license.key /opt/memclaw/license/license.key
# That is the whole action: both services re-verify hourly.
# To take effect now instead, restart the two that hold the license —
# but note a restart refuses to start on an invalid key, whereas the
# hourly loop keeps serving the cached one.
docker compose restart platform-admin-api platform-auth-api
```

## Upgrades

See `upgrade.md` — air-gap upgrades follow the same pattern: load the new
release tarball with `airgap-load.sh`, bump `CAURA_VERSION=vX.Y.Z` in
`/opt/memclaw/.env`, run `docker compose up -d`.
