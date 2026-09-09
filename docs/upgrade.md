# Upgrade guide

Upgrades are **tag-driven** and fully reversible. Bump `CAURA_VERSION`
in `.env`, pull the new images, restart. Data stays in Docker named
volumes — never touched by the upgrade.

> **Operators / Caura-managed fleet:** if the deployment fronts the stack with
> the TLS (Caddy) overlay or runs the scheduler services (`core-operations`,
> `platform-operations`), follow the
> [operator runbook](upgrade-runbook-operator.md) instead — it covers the
> explicit `-f` overlay files, staging-first validation, and the scheduler
> images. The flow below is the minimal customer path.

## Supported upgrade path

**Sequential minor versions only.** v1.0.x → v1.1.x, then v1.1.x → v1.2.x.
Skipping minors (v1.0.x → v1.2.x) is not tested and may leave Alembic
migrations in an inconsistent state.

Patch releases (v1.0.0 → v1.0.1) can always be jumped without
intermediate steps.

## Prerequisites

- Working install (if the current stack is down, fix that first; an
  upgrade won't repair a broken deployment).
- Backup from the last 24h (`./scripts/backup.sh` — see `day2-ops.md`).
- The target release tarball (`memclaw-onprem-<new-version>.tar.gz`) if
  you're air-gapped.

## Connected upgrade

```bash
cd /opt/memclaw

# 1. Take a safety backup
./scripts/backup.sh

# 2. Pick the target version
./scripts/set-version.sh v1.1.0

# 3. Pull + roll
docker compose pull
docker compose up -d

# 4. Watch migrations run
docker compose logs -f platform-storage-api
# → look for "alembic upgrade head" and "Application startup complete"
```

> `set-version.sh` ships in the release bundle. On an install that predates it
> the file will not be there — open `.env` and change the version key by hand
> instead. It is `CAURA_VERSION`, or the older `MEMCLAW_VERSION` if your file  <!-- legacy-name-ok: teaches the old spelling so an operator recognises which key their file has -->
> was written before the rename; both are read, and
> [`env-aliases.md`](env-aliases.md) lists every pair.

Migrations auto-run on `platform-storage-api` startup (idempotent — the
runner is safe to re-invoke on restart). Other services won't accept
traffic until storage-api reports healthy.

## Air-gap upgrade

Identical flow, with two extra steps at the front:

```bash
# 1. Load the new images alongside the old ones
./airgap-load.sh /path/to/memclaw-onprem-v1.1.0.tar.gz

# 2–4. Same as connected, but `docker compose pull` is a no-op — the
#      airgap overlay resolves to locally-loaded tags.
cd /opt/memclaw
./scripts/backup.sh
./scripts/set-version.sh v1.1.0
docker compose -f docker-compose.yml -f docker-compose.airgap.yml up -d
```

The old images stay on disk. Once you've verified the new version,
clean them up:

```bash
docker image prune  # removes unreferenced images only
```

## Verify after upgrade

```bash
# Version endpoint reflects the new tag
curl -sf http://caura.acme.com/api/version | jq .version
# → "v1.1.0"

# License still valid
curl -s http://caura.acme.com/api/license/status \
  -H "Authorization: Bearer $JWT" | jq .severity
# → "ok"

# Write a test memory + search for it (round-trip through the full stack)
curl -s -X POST http://caura.acme.com/api/memories \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"tenant_id":"...","agent_id":"...","content":"upgrade-smoke","memory_type":"fact"}'
# → 201

# All containers healthy
docker compose ps
```

## Rollback (connected)

A rollback is an upgrade toward the older tag, so on a connected install it
goes through `upgrade.sh` either way: both spellings of the pinned version key
move, a snapshot is taken, and the result is health-checked. The success banner
prints the route your host can take, so after a live upgrade you can paste what
it gave you.

**Air-gapped installs are different and have their own section below** —
`upgrade.sh` fetches the bundle and pulls images, so it needs the network for
any run, rollback included.

**If `upgrade.sh` is on disk**, which it is on any host that has upgraded
before:

```bash
cd /opt/memclaw
sudo bash ./upgrade.sh --to v1.0.0
```

`bash ./upgrade.sh` rather than `./upgrade.sh`: a copy fetched with `curl -o` is
readable but not executable, and nothing in the install sets the bit.

If you installed somewhere other than the path above, add
`--memclaw-home /your/root`. The script resolves the install root from its  <!-- legacy-name-ok: the install-root flag, named as the script names it -->
environment or that flag and never from the working directory, so the `cd`
above does not tell it where to look — and the banner printed at the end of an
upgrade already includes the flag with your root filled in.

**Otherwise fetch it.** This needs nothing installed and is what the banner
falls back to:

```bash
curl -fsSL https://onprem.caura.ai/upgrade.sh | sudo bash -s -- --to v1.0.0
```

`-f` matters here rather than being habit: without it, an error page from a
broken endpoint is piped into `sudo bash` instead of `curl` failing.

**If the operator CLI is installed**, `memclawctl rollback` is a shorthand for  <!-- legacy-name-floor: the shipped CLI's own command; an install whose CLI predates the alias has only this spelling -->
the first form, and it picks the version for you: it reads the
`.memclaw-prev-version` marker that `upgrade.sh` writes before it changes  <!-- legacy-name-floor: the on-disk marker file upgrade.sh writes -->
anything, then re-runs this script with `--to <that version>`. Do not prefix it
with `sudo` — `upgrade.sh` elevates itself when it cannot reach the Docker
daemon, whereas `sudo` in front of the CLI looks for the CLI on root's `PATH`,
where a per-user install is not.

The banner does not try to detect the CLI, for that same reason: it prints while
running as root, where a per-user install is invisible.

On a connected install, do not edit the version in `.env` by hand instead —
which older revisions of this page suggested. Going through `upgrade.sh` moves
both spellings of the key, snapshots first and health-checks after; a hand edit
does none of that and can fail silently, as the next section explains.

## Rollback (air-gapped)

`upgrade.sh` is not usable here, for the same reason it is not in the air-gap
*upgrade* flow above: it refreshes the bundle over the network and runs
`docker compose pull`, and it exits rather than continuing when either fails.
So an offline rollback is the manual route, and it is the mirror image of the
air-gap upgrade.

**It only works if the old images are still loaded.** `docker image prune`
after a successful upgrade is what removes them, so if that has already run,
load the previous release tarball first with `./airgap-load.sh`.

From the install root:

**1. Find out which spelling of the version key your `.env` actually carries**
— run `grep -nE '^(CAURA|MEMCLAW)_VERSION=' .env`. Do not skip this and reach  <!-- legacy-name-ok: names both spellings of the version key, which is the whole point of the command -->
for a `sed`: a pattern anchored on the spelling the file does *not* have
matches nothing, exits 0 and prints nothing. `up -d` then re-resolves to the
tag already running, every health check passes because nothing changed, and the
rollback reports success without having happened.

**2. Set every line that printed to the old tag.** If both are present they
must agree — the newer spelling is read first, so a half-edited pair resolves
to the stale one.

**3. Roll**, with the overlay that resolves images locally, then **4. verify**
rather than assume:

```bash
docker compose -f docker-compose.yml -f docker-compose.airgap.yml up -d
docker compose ps
curl -sf http://<your-host>/api/version | jq .version
```

Step 4 is not ceremony. The failure mode this procedure has, and that
`upgrade.sh` exists to remove on connected hosts, is reporting success while
running the version you were trying to leave.

**Caveat**: Alembic doesn't auto-downgrade. If the new version shipped a
destructive migration, rollback requires restoring from the backup you
took in step 1:

```bash
./scripts/restore.sh /opt/memclaw/backups/memclaw-backup-<last-good>.tar.gz
```

Caura commits to making minor-version migrations backwards-compatible
within the same major. Cross-major migrations (v1.x → v2.x) will call
this out in the release notes.
