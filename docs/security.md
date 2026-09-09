# Security model — Caura on-prem

Reference for customer CISOs, SOC teams, and auditors. Scope: the
on-prem deployment as shipped by this bundle, not Caura's hosted service.

## Trust boundaries

```
┌──── customer network ─────────────────────────────────────────────┐
│                                                                   │
│  agents / OpenClaw clients                                        │
│        │ X-API-Key (agent or tenant scope)                        │
│        ▼                                                          │
│  [ gateway (port 80) ]                                            │
│        │ auth_request → auth-api                                  │
│        │ inject X-Tenant-ID + X-Org-Read-Only                     │
│        ▼                                                          │
│  ┌──────────┬──────────┬──────────┬──────────┬──────────┐         │
│  │ auth-api │admin-api │storage-  │audit-api │ core-api │         │
│  │          │          │  api     │          │          │         │
│  └────┬─────┴────┬─────┴────┬─────┴────┬─────┴────┬─────┘         │
│       │          │          │          │          │               │
│       ▼          ▼          ▼          ▼          ▼               │
│     postgres  redis     rabbitmq                                  │
│                                                                   │
│  license.key  (RS256 JWT, mounted read-only into auth/admin-api)  │
│                                                                   │
└───────────────────────────────────────────────────────────────────┘
           ▲                                         │
           │ TLS terminated by customer's reverse    │ optional daily
           │ proxy (Caddy, Traefik, cloud LB) —      │ phone-home to
           │ gateway speaks plain HTTP internally    │ ops.caura.ai
           │                                         │ (HMAC-signed,
                                                     │ opt-out per license)
```

Everything inside the box is the customer's trust domain. The bundle
ships no outbound dependencies except the optional phone-home
(off by default on air-gap licenses).

## Data at rest

| Store         | Encryption | Notes |
|---|---|---|
| Postgres      | customer-managed (filesystem encryption / cloud disk) | no app-level DB encryption |
| Redis         | customer-managed                                        | STM + read-only status cache only — no persistent data |
| RabbitMQ      | customer-managed                                        | audit event queue; messages ephemeral |
| tenant settings | **Fernet (AES-128-CBC + HMAC-SHA256)**               | encrypted via `SETTINGS_ENCRYPTION_KEY` at the application layer — the key is in `.env`, rotate by re-keying and re-encrypting (tool shipping in a later version) |
| license.key   | signed + timestamped, not encrypted                     | contents are not sensitive; signature is verified at every service start + hourly thereafter |
| backups       | `pg_dump -Fc` + Redis RDB + config; customer-encrypted  | `scripts/backup.sh` produces a tarball; encrypt at the customer's choice of tool |

## Authentication + authorization

- **Users** — Argon2id password hashes, optional GitHub OAuth, optional
  TOTP 2FA.
- **Agents / API clients** — SHA-256-hashed API keys (prefix `mc_`), 
  scope is a single tenant. Rotated via `cauractl issue-api-key`.
- **License-driven read-only**: when the license is expired or the org
  is explicitly marked read-only, the gateway's `auth_request` surfaces
  an `X-Org-Read-Only: true` header that `enforce_usage_limits()` in
  core-api translates to HTTP 403 on every write. Reads unaffected.

Auth flow at the wire:
1. Client hits `/api/*` with `X-API-Key` or `Authorization: Bearer <JWT>`.
2. Gateway's `auth_request` subrequests auth-api's `/api/v1/auth/validate`.
3. auth-api validates (SHA-256 hash lookup or JWT signature), computes
   read-only state (in-memory 30s cache per tenant), returns the
   identity headers.
4. Gateway forwards the request + identity headers to the upstream; the
   raw API key is **stripped** on the `/mcp` path so core-api never
   sees it.

## License mechanism

- **RS256 JWT** signed by CauraOps. Public key baked into every
  service image at `/app/common/license/keys/license_public.pem`.
- Claims include: `license_id`, `org_name`, `issued_at`, `expires_at`,
  per-limit caps (`max_users`, `max_writes_per_month`, etc.), `features`
  list, `phone_home_enabled` flag.
- Loader fails **closed** on signature mismatch (service refuses to
  start — the customer cannot edit or forge a license).
- Expiry has a **24-hour clock-drift tolerance** so NTP-adjacent
  glitches don't lock people out. Past 24h, the service flips to
  read-only until a valid license is dropped in.
- **Tampering** = loud failure at startup. `caura_write` returns 403
  within 30s of the license flipping via the existing 30s read-only cache.

## Phone-home (optional)

Only applies when `license.phone_home_enabled = true`. Air-gap licenses
are issued with this disabled; the on-prem client skips the scheduled
task entirely.

When enabled:
- HTTPS POST to `https://ops.caura.ai/api/onprem/heartbeat` once per 24h
- Body is aggregates only: writes/searches/recalls counters, current
  version, host OS family — **no tenant names, no memory contents, no
  user PII**
- Body is signed with HMAC-SHA256 using a key derived from the
  license_id + issued_at (both also in the JWT) — receiver verifies
  before recording
- Failures are silent + retried with jittered backoff. Never blocks
  the stack.

## Threat model — what we defend against

| Threat | Mitigation |
|---|---|
| License forgery | RS256 signature (private key is in CauraOps GCP Secret Manager, never leaves that boundary) |
| License tampering | Loader refuses to load on any signature mismatch, service doesn't start |
| License replay past expiry | Expiry enforced with 24h clock-drift tolerance; after that, writes return 403 |
| Stolen API key | Keys are scoped per-tenant; rotation via `cauractl issue-api-key`; revoke via admin-api |
| Token exfiltration via logs | JWTs + API keys are masked in structured logs (prefix-only) |
| Phone-home replay / spoof | HMAC-SHA256 over raw body with a key unique per-license; constant-time verify on CauraOps side |
| Malicious plugin source | `install-plugin` / `plugin-source` serve files from the committed `plugin/` tree — verify the `plugin-source-hash` matches on the client if you want an integrity gate |
| Gateway bypass | All `/api/*` routes are behind the single gateway; services are on an internal Docker network with no published ports by default (only `gateway:80` is exposed) |
| Privilege escalation within tenant | RBAC: `role ∈ {owner, admin, member}`; admin-api enforces org_role on every mutation; superadmin is cross-tenant only via explicit `super_admin` JWT claim |

## Threat model — what we DO NOT defend against

- **Compromised customer VM**: if an attacker has root on the VM, all
  bets are off (they can read `.env`, license, DB files).
- **Malicious operator with superadmin access**: internal threat; rely
  on your own SSO / IAM controls.
- **DoS**: no rate limiting in the on-prem gateway by default (no L4 DDoS
  protection). Put it behind a Caddy/Cloudflare/nginx+fail2ban if the
  stack is publicly reachable.
- **Supply chain**: images are pulled from `ghcr.io/caura-ai/*`; verify
  provenance with `cosign verify` (signatures are keyless via sigstore
  OIDC) — we sign + publish SBOMs per release but don't re-verify
  runtime.

## Compliance posture

Not formally certified. Architecture is compatible with:
- **SOC 2 Type II**: audit trail (`platform-audit-api` + `enterprise.audit_log`),
  RBAC, at-rest encryption (customer-managed), password hashing.
- **HIPAA**: BAA needed; on-prem removes the hosted-service data-sharing
  boundary, but the customer assumes all PHI responsibilities.
- **FIPS 140-2**: Python `cryptography` + `python-jose` use OpenSSL;
  use a FIPS-validated OpenSSL build in the container base image if
  required. Not shipped as a default.

## Reporting vulnerabilities

`security@caura.ai` — GPG key at https://caura.ai/.well-known/security.txt.
Coordinated disclosure, 90 days.
