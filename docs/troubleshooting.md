# Troubleshooting

Every entry here corresponds to a real failure we caught on a test VM
during build-out. Symptoms + root cause + fix.

## install.sh — "Cannot reach Docker daemon"

```
ERROR Cannot reach Docker daemon. Either run as root, add yourself
to the docker group (and re-login), or install sudo.
```

**Root cause**: Docker CLI is on PATH but your user isn't in the
`docker` group, and the installer couldn't find `sudo` to self-elevate.

**Fix**: add yourself to the docker group:

```bash
sudo usermod -aG docker $(whoami)
newgrp docker
# OR log out + back in for the group to take effect
```

Since v1.0.1 `install.sh` auto-re-executes through sudo, so this is
almost always a missing-sudo on a minimal image. `apt-get install -y sudo`
and retry.

## install.sh — "--offline: upstream base images missing"

```
ERROR --offline: upstream base images missing: pgvector/pgvector:pg16
redis:7-alpine. Run airgap-load.sh first.
```

**Root cause**: `--offline` assumes you've run `airgap-load.sh`; you
haven't or it failed.

**Fix**:

```bash
./airgap-load.sh /path/to/memclaw-onprem-<version>.tar.gz
# verify
docker images | grep -E '^pgvector|^redis|^rabbitmq'
./install.sh --offline ...   # retry
```

## Gateway returns 502 Bad Gateway

**Symptoms**:
```
< HTTP/1.1 502 Bad Gateway
< server: nginx/1.27.5
```

Gateway logs show `connect() failed (111: Connection refused)` or
`(113: Host is unreachable)` against an internal IP like `172.18.0.11`.

**Root cause #1 — backend is crashing**. The gateway keeps running
because nginx has no upstream health-check; it forwards, gets refused,
502s the client. Check which backend is down:

```bash
docker compose ps
# → platform-admin-api   Restarting (1)
docker compose logs platform-admin-api | tail -30
```

Common reasons a platform service won't start:
- `JWT_SECRET` or `POSTGRES_PASSWORD` missing in `.env` (compose refuses
  to render — you'll see an error before containers even start)
- `SETTINGS_ENCRYPTION_KEY` missing — `core-api` and `platform-admin-api`
  refuse to boot in `ENVIRONMENT=production` without it. `install.sh`
  auto-generates it; if you wrote the `.env` manually, add one:
  ```bash
  python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
  ```
- License file missing at `/etc/memclaw/license.key` — `platform-auth-api`
  + `platform-admin-api` refuse to start when `LICENSE_FILE` is set but
  the file doesn't exist.

**Root cause #2 — nginx cached a stale upstream IP**. Happens after a
backend container restart changed its IP but nginx still has the old
one. Fixed in v1.0.1 with `resolver 127.0.0.11 valid=10s` + per-request
DNS resolution. On older builds, restart the gateway:

```bash
docker compose restart gateway
```

## License won't load — "License file not found"

```
common.license.verifier.LicenseMissingError:
License file not found at /etc/memclaw/license.key.
```

**Root cause**: the license volume mount is empty or the file has the
wrong name.

**Fix**:

```bash
ls -la /opt/memclaw/license/
# → license.key must be present, readable
# → NOT license.key.txt, NOT license.json — just "license.key"

# If missing, copy yours in:
sudo cp ~/license.key /opt/memclaw/license/license.key

# Permissions: 644 is fine; nginx doesn't need the file, only the
# admin-api + auth-api containers do, and they bind-mount it ro.
```

## License loaded but `severity=expired`

**Normal behaviour** if the license is past its expiry date. Writes
return HTTP 403 with `"Organization is in read-only mode"`, reads stay
available.

**Fix**: request a renewed license from your Caura representative, drop
it in:

```bash
sudo cp renewed-license.key /opt/memclaw/license/license.key
```

That copy is the whole action. Both services re-verify on an hourly loop,
so the new key takes effect within 60 minutes and nothing else is needed.
`cauractl license load <path>` from the host does the same copy and no
more — despite the name, it does not reload anything.

**To make it take effect now**, restart the two services that hold the
license:

```bash
docker compose restart platform-admin-api platform-auth-api
```

Choose between those two paths deliberately, because they fail in
opposite directions. The hourly loop is **fail-safe**: if the new file is
missing or unreadable it keeps serving the cached license and starts
polling every ~30s instead of flipping the org read-only. A restart is
**fail-fast**: the license is read during startup and a service refuses
to start on an invalid one. So if you are confident in the key, restart;
if you are not, let the loop pick it up and stay up either way.

**Clock-drift tolerance**: if the license is expired by ≤24h, the
loader treats it as still valid (NTP sanity buffer). Past 24h it flips
to expired.

## Writes return 403 but license is valid

```
{"detail":"Organization is in read-only mode:
usage exceeds plan limits. Upgrade your plan or delete data..."}
```

**Two possible causes**:

1. **License genuinely exceeded** (e.g. `max_writes_per_month` hit).
   Check usage:
   ```
   curl -s http://host/api/license/status -H "Authorization: Bearer $JWT"
   ```
   The `limits` section shows the caps; compare with
   `/api/superadmin/tenants` usage counters.
2. **Org-level read-only flag set by ops** (cancellation, investigation,
   etc.). SaaS-inherited feature; on-prem rarely hits this.

## Frontend shows the wrong URL (memclaw.net instead of customer hostname)

**Root cause**: the app-frontend image was built with a baked
`NEXT_PUBLIC_SITE_URL` that doesn't match your customer domain, OR the
runtime-config shim (`/env-config.js`) isn't being served.

**Fix**: verify the shim is live:

```bash
curl -s http://caura.acme.com/env-config.js
# → window.__MEMCLAW_CONFIG__ = {
# →   apiUrl: "https://caura.acme.com",
# →   siteUrl: "https://caura.acme.com",
# →   billingEnabled: false,
# → };
```

If the response looks hardcoded (still says "memclaw.net"), check
docker-compose.yml sets `MEMCLAW_API_URL` / `MEMCLAW_SITE_URL` / 
`MEMCLAW_BILLING_ENABLED` on the `app-frontend` service and restart:

```bash
docker compose restart app-frontend
```

> **These three keep the old spelling on purpose.** Every other setting in this
> installer also answers to a `CAURA_*` name ([`env-aliases.md`](env-aliases.md)),
> but these are read *inside* the application image, which is built in another
> repo and ships on a tag customers have already pulled. A `CAURA_*` spelling
> here would name something no released image reads, so the frontend would come
> back up with no config at all. Do not "finish" the rename on these — they move
> when the application image moves.

## Phone-home heartbeats never reach ops.caura.ai

Connected deployments only — air-gap installs have `phone_home_enabled=false`
baked into the license and never attempt a connection.

**Diagnostic**:
```bash
docker compose logs platform-admin-api | grep phone_home
# → looking for phone_home_ok OR phone_home_network_error OR phone_home_non_2xx
```

**Common causes**:

- **Firewall blocking outbound 443 to `ops.caura.ai`**: expected on
  locked-down networks. Either open egress to `ops.caura.ai:443`, or ask
  Caura to re-issue the license with `phone_home_enabled=false`.
- **Expired DNS / TLS issue**: `phone_home_network_error: <detail>` in
  the logs will show the exact failure.
- **Signature rejected**: `phone_home_non_2xx: status=401 body=Invalid
  signature`. Means the license on disk doesn't match what CauraOps has
  recorded — typically because the license was revoked. Contact Caura.

Phone-home failures **never affect service availability** — the loop
retries with exponential backoff and the rest of the stack runs normally.

## "Address already in use" on port 80 / 443 during install

**Root cause**: something else on the host is already bound to 80 (most
likely Apache or the host's nginx).

**Fix**: either disable the other service, or change the gateway port
mapping in `/opt/memclaw/.env`:

```bash
GATEWAY_PORT=8080   # then docker compose up -d
```

And update DNS / the upstream load balancer to point at the new port.

## Postgres disk filling up

Memory data lives in the `pgdata` named volume. Check:

```bash
docker system df -v | grep pgdata
```

Growth rate is customer-specific; typical memory rows are ~2-5 KB. If
you're approaching the disk limit:

1. Prune old memories via `/api/admin/memories/cleanup` (age-based) or
   by raising the license's `max_memories` cap (requires Caura-issued
   renewal).
2. Extend the VM disk + `parted`/`resize2fs` the root partition — Docker
   volumes pick up the new space automatically.

## Logs don't appear in /opt/memclaw/logs/

```
ls /opt/memclaw/logs/core-api/
# → (empty)
```

**Root cause**: either `LOG_FILE` isn't set in the service's environment
(stdout-only mode, SaaS default) or the container can't write to the
bind-mounted dir.

**Diagnostic**:

```bash
# Is the env var set in the running container?
docker compose exec core-api printenv LOG_FILE
# Expected: /var/log/memclaw/core-api/core-api.log

# Is the dir writable from the container's UID?
docker compose exec core-api ls -la /var/log/memclaw/core-api/
# → drwxrwxrwx ... or owned by appuser
```

**Fix**: install.sh creates the dirs `0777` so any container UID can
write. If you set up the stack by hand, recreate them:

```bash
for svc in platform-{storage,auth,admin,audit}-api core-{storage,}api gateway; do
  sudo mkdir -p /opt/memclaw/logs/$svc
  sudo chmod 0777 /opt/memclaw/logs/$svc
done
docker compose restart
```

See [`logging.md`](logging.md) for the full log layout.

## Support bundle: "Leak-scan: FAILED"

```
cauractl support review /tmp/*-support-*.tar.gz
# → logs/core-api/... → openai_key: sk-proj-AAAAAAAAAAAAAAAA
```

**Root cause**: the redactor missed a shape that the scanner caught —
usually because a library logged a secret in a format we don't
recognise (e.g. a dict repr with unusual quoting).

**Fix**:

1. Upgrade `cauractl` to the latest release and re-bundle —
   redactor patterns get tightened over time.
2. If the bundle is genuinely clean after manual review of the flagged
   lines (many `high_entropy_secret` hits are false positives against
   base64-encoded hashes or fingerprints), add `--skip-leak-scan` to
   `support upload` after auditing.
3. Open a ticket so we can add the missing pattern.

## Admin creation fails with HTTP 422 (invalid email)

`install.sh` ends with `ERROR /setup/admin failed` and a 422 like:

```
value is not a valid email address: The part after the @-sign is a
special-use or reserved name that cannot be used with email.
```

The admin email uses a **reserved TLD** (`.local`, `.test`, `.example`,
`.invalid`, or `localhost`). Email validation rejects these. Use a real,
deliverable domain — `admin@yourcompany.com`, not `admin@caura.local`.
The VM's `--hostname` can still be anything (it's only the `server_name`);
this constraint applies only to the admin email.

```sh
# fix and re-run admin creation (stack is already up):
curl -sk -X POST https://$PUBLIC_HOSTNAME/api/setup/admin \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@yourcompany.com","password":"<strong>","org_name":"yourorg"}'
```

## Recall returns `503 Embedding service unavailable`

Memory *writes* succeed (embedding is deferred) but *recall* / semantic
search 503s. The stack has no reachable embedding backend:

- **Connected installs:** set `EMBEDDING_PROVIDER=openai` (or anthropic /
  gemini) and a valid API key in `/opt/memclaw/.env`, then
  `docker compose up -d core-api`. Confirm the provider is reachable from
  the VM (egress / proxy).
- **Local embeddings:** requires the bundled embedder image — see
  `install-airgap.md` for availability and how to enable it.

## Getting more help

- `docker compose logs --timestamps --tail 200 <service>` for ANY
  service issue.
- `cauractl status` for a quick license + setup snapshot.
- `cauractl support bundle` to snapshot logs + compose state into a
  redacted tarball. Attach to a support email, or upload directly:
  `cauractl support upload /tmp/*-support-*.tar.gz`. See
  [`logging.md`](logging.md) for what's included and the redaction
  guarantees.
- Contact: your Caura rep or `support@caura.ai`. Include:
  - `cauractl license status` output
  - `docker compose ps` output
  - A support bundle (preferred over raw logs — pre-redacted)
