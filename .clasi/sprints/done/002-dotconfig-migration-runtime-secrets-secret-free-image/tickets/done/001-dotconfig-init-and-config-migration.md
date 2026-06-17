---
id: '001'
title: dotconfig init and config migration
status: done
use-cases:
- SUC-001
depends-on: []
github-issue: ''
issue: dotconfig-migration.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# dotconfig init and config migration

## Description

Replace the git-crypt secrets setup with dotconfig (SOPS + age). This ticket
covers initialising the dotconfig layout, migrating all existing config files
into it, and removing git-crypt from the repository.

**Note**: `config/dotconfig.yaml` already exists (version `0.20260616.4`),
indicating dotconfig has been partially initialised. Begin by checking whether
`config/sops.yaml` also exists before running `dotconfig init` again; if the
SOPS config is present, the init step may be a no-op or just needs key
registration.

## Acceptance Criteria

- [x] `config/sops.yaml` exists and lists the correct age public key
      fingerprint for encryption.
- [x] All non-secret config values are present in `config/devel/public.env`,
      `config/prod/public.env`, and `config/local-prod/public.env` (or local
      user equivalents under `config/local/{user}/`).
- [x] All secret config values are present in SOPS-encrypted
      `config/devel/secrets.env` and `config/prod/secrets.env` (and
      `config/local-prod/secrets.env` if applicable).
- [x] `dotconfig load -d devel -o /tmp/test-devel.env` runs without error and
      produces a readable env-file containing `DATABASE_URI`, `DOCKER_URI`,
      and OAuth credentials.
- [x] `dotconfig load -d prod --no-export -e -o /tmp/test-prod.env` runs
      without error; the output contains `ID_RSA=<base64 string>` and no
      `export ` prefixes.
- [x] The git-crypt filter line (`**/secrets/* filter=git-crypt diff=git-crypt`)
      is removed from `.gitattributes`.
- [x] Raw secret files in `config/secrets/` (prod.env, devel.env, docker.env,
      local-prod.env, id_rsa, id_rsa.pub, secret.env) are removed from git
      tracking (`git rm --cached`) and added to `.gitignore` (or the directory
      is removed from tracking entirely).
- [x] `dotconfig audit` exits 0 (no unencrypted secrets detected).
- [x] `.env` is listed in `.gitignore` (the runtime env-file must never be
      committed).

## Implementation Plan

### Approach

1. Check for `config/sops.yaml`. If absent, run `dotconfig init` to generate
   the age keypair and create `config/sops.yaml`. If present, verify the age
   key fingerprint matches the local key.

2. Create the dotconfig directory layout:
   ```
   config/devel/public.env
   config/devel/secrets.env       (will be SOPS-encrypted)
   config/prod/public.env
   config/prod/secrets.env        (will be SOPS-encrypted)
   config/local-prod/public.env   (if needed, else config/local/{user}/)
   config/local-prod/secrets.env  (SOPS-encrypted)
   ```

3. Migrate values from the current files into the new layout:
   - `config/config.env` → shared non-secret values → `config/devel/public.env`
     and `config/prod/public.env` (or a shared base if dotconfig supports it).
   - `config/prod.env` → prod-specific non-secrets → `config/prod/public.env`.
   - `config/secrets/secret.env` → shared secrets → respective `secrets.env`.
   - `config/secrets/prod.env` → prod secrets → `config/prod/secrets.env`.
   - `config/local-prod.env` → local-prod values → `config/local-prod/` or
     `config/local/{user}/`.
   - Add `ID_RSA_FILE=id_rsa` to `config/prod/public.env` (or secrets.env if
     the path itself is sensitive — likely public) so `dotconfig load -e` picks
     it up for base64 embedding.

4. Run `dotconfig save` to encrypt the secrets.env files via SOPS.

5. Remove git-crypt:
   - Delete the filter line from `.gitattributes` (if it still reads
     `**/secrets/* filter=git-crypt diff=git-crypt`, remove it entirely).
   - `git rm --cached config/secrets/prod.env config/secrets/devel.env
     config/secrets/docker.env config/secrets/local-prod.env
     config/secrets/secret.env config/secrets/id_rsa config/secrets/id_rsa.pub`
   - Optionally `git rm --cached` the entire `config/secrets/` directory.
   - Add `config/secrets/` (or specific files) to `.gitignore`.

6. Add `.env` to `.gitignore`.

7. Run `dotconfig audit` to confirm zero unencrypted secrets.

### Files to Modify

- `.gitattributes` — remove git-crypt filter line
- `.gitignore` — add `config/secrets/` and `.env`
- `config/sops.yaml` — created by `dotconfig init` if not already present

### Files to Create

- `config/devel/public.env`
- `config/devel/secrets.env`
- `config/prod/public.env`
- `config/prod/secrets.env`
- `config/local-prod/public.env` (or `config/local/{user}/public.env`)
- `config/local-prod/secrets.env` (SOPS-encrypted)

### Files to Remove from git Tracking

- `config/secrets/prod.env`
- `config/secrets/devel.env`
- `config/secrets/docker.env`
- `config/secrets/local-prod.env`
- `config/secrets/secret.env`
- `config/secrets/id_rsa`
- `config/secrets/id_rsa.pub`

### Testing Plan

- Run `dotconfig load -d devel -o /tmp/test-devel.env` and inspect output.
- Run `dotconfig load -d prod --no-export -e -o /tmp/test-prod.env` and
  confirm `ID_RSA=<base64>` is present and no `export ` prefix appears.
- Run `dotconfig audit` and confirm exit 0.
- Confirm `git status` does not show any files in `config/secrets/` as tracked.

### Rollback Note

Before removing tracked files, tag the current commit as `pre-dotconfig-migration`
so the old state is recoverable. The git-crypt-encrypted blobs on the remote
remain accessible as long as no force-push is done.

### Documentation Updates

Add a brief comment in `config/sops.yaml` or a `config/README.md` (if one
exists) explaining the new layout and the `dotconfig load` command required
before starting the app or deploying.
