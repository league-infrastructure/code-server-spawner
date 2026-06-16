---
status: pending
---

# Migrate config to dotconfig (SOPS-encrypted cascade) and wire into deployment

## Context

The project should manage configuration through `dotconfig` (installed at
`/Users/eric/.local/bin/dotconfig`; `sops` + `age` available). dotconfig stores
a layered cascade with **SOPS-encrypted secrets**, so secrets never sit in git
as plaintext. Run `dotconfig --instructions` for the full agent manual.

This is the structural follow-up to [[purge-secrets-from-git-history]] and must
land **after** that purge (so we don't re-commit plaintext mid-migration).

## The mismatch to reconcile

dotconfig layout (target):
```
config/{deploy}/public.env      # shared non-secret
config/{deploy}/secrets.env     # SOPS-encrypted
config/local/{user}/public.env  # per-dev overrides
config/sops.yaml
```
Current app layout (what `get_config()` in `cspawn/util/config.py` reads):
```
config/config.env
config/{deploy}.env             # devel | prod | local-prod
config/secrets/secret.env
config/secrets/{deploy}.env
```
These are different conventions. "Linking into the deployment" = making the app
consume dotconfig's output.

## Scope

1. `dotconfig init` — create the `config/` structure, generate/age keypair,
   write `config/sops.yaml`. Reconcile with the existing `config/` tree.
2. Migrate each deployment (devel, prod, local-prod) into
   `config/{deploy}/public.env` + SOPS-encrypted `secrets.env`. Move the
   per-developer bits (e.g. local DOCKER_URI) into `config/local/{user}/`.
3. **Wire the app to dotconfig.** Two options to decide during planning:
   (a) keep `get_config()`'s cascade but have it read the new paths, or
   (b) have the build/run step run `dotconfig load -d <deploy>` to produce a
   single `.env` the app sources. Option (b) is closer to dotconfig's design
   and how the Docker deploy already wants an env-file (`docker stack deploy`
   needs `--no-export`). Verify against `cspawn/util/config.py` precedence
   (later files / os.environ win).
4. Deploy wiring: generate the prod `.env` via
   `dotconfig load -d prod --no-export -o .env` for the Docker stack; consider
   `dotconfig gh-push` for GitHub Actions / Codespaces secrets.
5. Install `dotconfig install-hooks` (audit pre-commit) and run
   `dotconfig audit` to confirm no unencrypted secrets remain.
6. The `_FILE`/`--embed` convention is useful for the SSH key and certs
   (base64 into env for Docker) — evaluate.

## Acceptance

- `dotconfig audit` reports zero unencrypted secrets.
- App loads correctly in devel, prod, and local-prod from the dotconfig
  cascade (login works, DOCKER_URI/DATABASE_URI/tokens resolve).
- Docker prod deploy consumes a dotconfig-generated env-file.
- Pre-commit audit hook installed.
- `cspawn/util/config.py` updated (or superseded) and documented.

## Risk

Touches live config loading for all deployments — a misconfig breaks app
startup. Plan carefully; verify each deploy env before switchover.
