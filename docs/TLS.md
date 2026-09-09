# TLS for on-prem Caura

**Default: TLS on.** A fresh `install.sh` run with no TLS-related
flags generates a self-signed cert and the gateway listens on 443,
redirecting port 80 traffic up. Customers who want a different posture
pick one of the patterns below.

## TL;DR — `install.sh` flags

```
# (default — no flag needed) self-signed cert in $CAURA_HOME/tls/
--tls-cert <pem> --tls-key <pem>    # bring your own (corporate CA, external Let's Encrypt)
--tls-domain <fqdn>                 # CN/SAN override; defaults to --hostname
--bind-address 127.0.0.1            # only listen on loopback (front with your own proxy)
--no-tls                            # opt out: HTTP-only. Required for internal-only or behind-proxy deployments.
                                    # (alias: --insecure-http, --acknowledge-insecure)
```

Reinstalling **doesn't** regenerate an existing self-signed cert; it
reuses what's at `$CAURA_HOME/tls/cert.pem` + `key.pem` so renewals
are explicit (delete the files, rerun `install.sh --tls-self-signed`).

---

## Pattern 1 — Front Caura with your existing TLS terminator

Recommended when you already run nginx, Traefik, Caddy, Cloudflare
Tunnel, an AWS ALB, etc. on the perimeter. Caura stays HTTP
internally; your terminator handles TLS.

Install:
```
curl -sL https://onprem.caura.ai/install.sh | sudo bash -s -- \
  --hostname caura.acme.com \
  --bind-address 127.0.0.1 \
  --no-tls
```

Your proxy:
- Listens on 443 with your cert.
- Sets `X-Forwarded-Proto: https`, `X-Forwarded-For`, `Host: caura.acme.com`.
- Proxies to `http://<host>:80` where Caura is listening on loopback.

Caura honours `X-Forwarded-Proto`, so generated URLs (license
download, plugin install command, etc.) come back with `https://`.

## Pattern 2 — Self-signed (auto, easiest)

Best for closed networks where you control the agents but don't have
public DNS or a corporate CA available.

Install:
```
curl -sL https://onprem.caura.ai/install.sh | sudo bash -s -- \
  --hostname caura.acme.com \
  --tls-self-signed \
  --tls-domain caura.acme.com
```

What happens:
- `openssl` generates an RSA-2048 cert valid 10 years with SAN
  `DNS:caura.acme.com, DNS:localhost, IP:127.0.0.1`.
- `cert.pem` + `key.pem` land in `$CAURA_HOME/tls/`.
- The gateway entrypoint detects them and switches to the TLS
  template — port 80 redirects to 443; HSTS for 30 days.

Caveats:
- Browsers and OpenClaw plugins won't trust the cert by default.
  Either install the cert into the OpenClaw VM's trust store, or
  set `NODE_TLS_REJECT_UNAUTHORIZED=0` in that VM's environment
  (downgrade — OK on a private network, never on the public
  internet).
- Cert renewal is manual: `rm $CAURA_HOME/tls/*.pem && install.sh --tls-self-signed`.

## Pattern 3 — Bring your own cert

Use when you have a corporate / internal CA that issues per-host
certs, or you've separately obtained one (e.g. Let's Encrypt via
your own ACME tooling).

Install:
```
curl -sL https://onprem.caura.ai/install.sh | sudo bash -s -- \
  --hostname caura.acme.com \
  --tls-cert /etc/ssl/caura/fullchain.pem \
  --tls-key  /etc/ssl/caura/privkey.pem
```

Renewal: drop the new cert in place and `docker compose restart gateway`.

---

## Plugin (OpenClaw node) configuration

Once Caura runs HTTPS, OpenClaw nodes should reach it over HTTPS too:

```
curl -s -X POST "https://caura.acme.com/api/v1/install-plugin" \
  -H "Content-Type: application/json" \
  -d '{"fleet_id":"prod","api_url":"https://caura.acme.com","api_key":"mc_..."}' \
  | bash
```

If the cert is self-signed, additionally trust it on the OpenClaw VM
before restarting the OpenClaw gateway:

```
sudo cp caura-cert.pem /usr/local/share/ca-certificates/caura.crt
sudo update-ca-certificates
systemctl --user restart openclaw-gateway
```

(or `NODE_TLS_REJECT_UNAUTHORIZED=0` for a quick development setup —
not for production).

## Pattern 4 — Let's Encrypt (automatic, recommended for public deployments)

Best for any deployment where the Caura VM has a publicly-resolvable
FQDN. Caddy sidecar handles ACME issuance + renewal automatically; no
manual cert juggling, ever.

Requirements:
- A DNS A/AAAA record pointing `caura.your-domain.com` at the VM's
  public IP.
- Port 80 reachable from the internet (HTTP-01 challenge). Open it in
  any firewall / cloud security group between the VM and the world.
- An email for ACME contact (renewal notices etc).

Install:
```
curl -sL https://onprem.caura.ai/install.sh | sudo bash -s -- \
  --hostname caura.acme.com \
  --tls-letsencrypt \
  --tls-domain caura.acme.com \
  --tls-email ops@acme.com
```

What happens:
- install.sh writes `$CAURA_HOME/caddy/Caddyfile` rendered from your
  domain + email.
- Adds `docker-compose.tls-letsencrypt.yml` overlay → Caddy sidecar on
  ports 80+443; gateway nginx loses its host port mapping (Caddy is
  the only thing the world talks to).
- Caddy provisions a Let's Encrypt cert on first start, renews 30
  days before expiry, no human intervention.

Browsers + plugins trust the cert natively. **No `/onprem-ca.pem`
auto-trust needed** — the install-plugin script's step 8 fail-path
runs (because we don't expose `/onprem-ca.pem` in this mode), no rc
edit, system trust handles everything.

Renewal: automatic. To manually trigger: `docker compose restart caddy`.

Caveats:
- Let's Encrypt rate-limits: max 50 certs / domain / week. Re-running
  install.sh in tight loops can hit the limit. To experiment without
  burning quota, edit `caddy/Caddyfile` and uncomment the
  `acme_ca https://acme-staging-v02.api.letsencrypt.org/directory`
  line, then `docker compose restart caddy`.
- Private / `*.local` / RFC1918 domains will be rejected at install
  time — they can't be issued by Let's Encrypt. Use Pattern 2
  (self-signed) for those.
