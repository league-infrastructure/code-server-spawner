---
status: in-progress
sprint: '002'
tickets:
- 002-001
- 002-002
- 002-003
- 002-004
---

# Migrate config from git-crypt to dotconfig; ship runtime env-file (no baked-in secrets)

## Context

Secrets are currently managed by **git-crypt** (`.gitattributes`:
`**/secrets/* filter=git-crypt`) — encrypted in git, decrypted in the working
tree. The Docker build then **bakes the decrypted plaintext into image layers**
via `COPY config/secrets/*` and `COPY config/secrets/id_rsa /root/.ssh/`
(`docker/Dockerfile` lines ~76-93). Two problems: git-crypt is being retired,
and secrets-in-image-layers is poor hygiene (anyone who can read the image on
swarm1 gets the SSH key + tokens).

**Target model (stakeholder-specified):**
1. Manage config with `dotconfig` (SOPS + age; `dotconfig --instructions` for the
   manual). Secrets live SOPS-encrypted, never as git-crypt blobs or plaintext.
2. At deploy time, assemble the full prod environment from dotconfig into a single
   env-file: `dotconfig load -d prod --no-export -o .env` (`--no-export` because
   `docker stack deploy` rejects `export KEY=` lines).
3. **Ship that env-file to the deployment and inject at RUNTIME** (stack `env_file:`
   / `--env-file`), NOT `COPY`'d into image layers. The image stays secret-free.

## Remove git-crypt

- Delete the `**/secrets/* filter=git-crypt diff=git-crypt` rules from
  `.gitattributes`.
- `git-crypt unlock` / export the files as plaintext, migrate them into dotconfig
  (which re-encrypts via SOPS), then stop tracking the raw secret files.
- Ensure no `GITCRYPT`-encrypted blobs remain expected by any tooling.

## SSH key handling — DECIDED: dotconfig --embed (base64 in env)

The app needs `id_rsa` at runtime to SSH to worker nodes. Env-files carry
KEY=value, not files, so use dotconfig's `_FILE`/`--embed` convention:
- Declare `ID_RSA_FILE=id_rsa` in the prod config; `dotconfig load -d prod -e`
  base64-encodes the (SOPS-decrypted) key into the env as `ID_RSA=<base64>`.
- The container **entrypoint** decodes `$ID_RSA` to `/root/.ssh/id_rsa`
  (chmod 600) at startup, then unsets it. Drop the `COPY ... id_rsa` lines from
  the Dockerfile.

## Layout to reconcile

dotconfig target: `config/{deploy}/public.env` + SOPS `config/{deploy}/secrets.env`,
`config/local/{user}/...`, `config/sops.yaml`.
Current app (`get_config()` in `cspawn/util/config.py`): `config/config.env` +
`config/{deploy}.env` + `config/secrets/secret.env` + `config/secrets/{deploy}.env`.

Decision for app wiring: prefer option (b) — the run/build step runs
`dotconfig load` to produce one `.env`, and the app sources that (matches the
deploy env-file flow). `get_config()` precedence (later files / os.environ win)
must be preserved or superseded cleanly.

## Scope / tasks

1. `dotconfig init` (age keypair, `config/sops.yaml`); reconcile existing tree.
2. Migrate devel / prod / local-prod into `config/{deploy}/{public,secrets}.env`;
   per-dev bits (local DOCKER_URI etc.) into `config/local/{user}/`.
3. Remove git-crypt (`.gitattributes`, unlock, stop tracking raw secrets).
4. App wiring: `dotconfig load`-generated `.env` consumed by the app; update or
   supersede `cspawn/util/config.py`.
5. Dockerfile: drop `COPY config/secrets/*` and `COPY ... id_rsa`. Add an
   entrypoint step that decodes `$ID_RSA` → `/root/.ssh/id_rsa`.
6. Stack (`docker/docker-stack.yaml`): add `env_file: .env` (or equivalent) for
   the `codeserver` service; deploy procedure generates `.env` via
   `dotconfig load -d prod --no-export -e -o .env` first.
7. `dotconfig install-hooks` (audit pre-commit) + `dotconfig audit` == 0 unencrypted.
8. Optional: `dotconfig gh-push` for GitHub Actions / Codespaces.

## Acceptance

- git-crypt fully removed; `dotconfig audit` reports zero unencrypted secrets.
- App loads in devel, prod, local-prod from dotconfig (login works; DOCKER_URI,
  DATABASE_URI, tokens, and the SSH key all resolve).
- Prod image contains **no** secrets in any layer (`docker history` / inspect);
  secrets arrive only via the runtime env-file + entrypoint-decoded key.
- Prod deploy works end-to-end via `dotconfig load` → env-file → `make up`.
- Pre-commit audit hook installed.

## Risk

Touches live config loading AND the deploy path for all environments. A misconfig
breaks app startup or worker-node SSH. Plan carefully; verify each deploy env
(and a real host-start, which exercises the SSH key) before switchover. Keep a
rollback to the current git-crypt image until verified.

## Related

- [[purge-secrets-from-git-history]] — NOTE: largely moot. Secrets were git-crypt
  encrypted on the remote all along (never plaintext-leaked), so a history purge
  is not a security necessity. Rescope that issue to credential rotation only
  (the creds were briefly decrypted locally), or close it.
