---
id: '002'
title: "dotconfig migration — runtime secrets, secret-free image"
status: planning-docs
branch: sprint/002-dotconfig-migration-runtime-secrets-secret-free-image
use-cases: [SUC-001, SUC-002, SUC-003, SUC-004]
issues: [dotconfig-migration.md]
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 002: dotconfig migration — runtime secrets, secret-free image

## Goals

Replace git-crypt secrets management with dotconfig (SOPS + age). Secrets are
never baked into image layers; they are assembled into a single env-file at
deploy time and injected at runtime. The SSH private key is delivered via a
base64-encoded env var decoded by the container entrypoint.

## Problem

Secrets are currently managed by git-crypt and COPY'd into Docker image layers
during build (`config/secrets/*`, `id_rsa`). This means anyone who can read the
image on the swarm has access to all credentials. git-crypt is being retired.

## Solution

1. `dotconfig init` — initialise age keypair, `config/sops.yaml`; migrate all
   config files from the current flat layout into the dotconfig layout
   (`config/{deploy}/public.env` + SOPS `config/{deploy}/secrets.env`).
2. Remove git-crypt from `.gitattributes`; stop tracking raw secret files.
3. Update `cspawn/util/config.py` so the app reads a dotconfig-generated `.env`
   (one file, produced by `dotconfig load`) for both local and prod environments.
4. Rewrite `docker/Dockerfile`: drop all `COPY config/secrets/*` lines; add an
   entrypoint shell wrapper that decodes `$ID_RSA` → `/root/.ssh/id_rsa`.
5. Update `docker/docker-stack.yaml` to add `env_file: .env` for the
   `codeserver` service; update `docker/Makefile` deploy flow to invoke
   `dotconfig load -d prod --no-export -e -o .env` before `make up`.
6. Install dotconfig pre-commit audit hook; verify `dotconfig audit` == 0.

## Success Criteria

- `dotconfig audit` reports zero unencrypted secrets anywhere in the repo.
- `docker history <image>` and `docker inspect` show no secrets in any layer.
- App loads and authenticates correctly in devel, prod, and local-prod
  environments (DATABASE_URI, DOCKER_URI, OAuth tokens, SSH key all resolve).
- A real code-server host-start works (SSH to worker node succeeds with
  the decoded key).
- Pre-commit hook blocks any future accidental plaintext commit.
- Rollback path documented: old git-crypt image tag retained until the new
  deploy is verified.

## Scope

### In Scope

- dotconfig init, sops.yaml, age keypair
- Migration of all existing config/secrets files to dotconfig layout
- git-crypt removal (.gitattributes, unlock, stop tracking)
- `cspawn/util/config.py` rewrite for dotconfig-generated `.env`
- `docker/Dockerfile` secret-COPY removal + entrypoint shell wrapper
- `docker/docker-stack.yaml` `env_file:` addition
- `docker/Makefile` pre-deploy `dotconfig load` step
- `dotconfig install-hooks` + audit
- Verification across devel, prod, local-prod

### Out of Scope

- `dotconfig gh-push` (GitHub Actions / Codespaces integration — optional, deferred)
- Git history purge / credential rotation (tracked separately as `purge-secrets-from-git-history`)
- Any application feature changes

## Test Strategy

- Manual verification: run `dotconfig load -d devel -o .env` and `dotconfig load -d prod --no-export -e -o .env`; inspect output.
- App smoke-test: start app in devel mode, verify login flow reaches the dashboard.
- Docker layer audit: `docker history <tag>` must show no secret values in any layer.
- SSH smoke-test: launch a code-server host; confirm SSH to worker node succeeds.
- Audit gate: `dotconfig audit` must exit 0.

## Architecture Notes

See `architecture-update.md` for the detailed module-level design.

Key decisions:
- Deploy model: assemble secrets into `.env` at deploy time via `dotconfig load`; inject at runtime via `env_file:`.
- SSH key: `ID_RSA_FILE=id_rsa` in dotconfig config → `dotconfig load -e` base64-encodes it; entrypoint decodes and writes `/root/.ssh/id_rsa`.
- App wiring: `get_config()` is superseded for prod/local-prod; app reads a pre-assembled `.env` via `dotenv_values('.env')` with `os.environ` override preserved.

## GitHub Issues

(None yet — no GitHub issues linked.)

## Definition of Ready

Before tickets can be created, all of the following must be true:

- [ ] Sprint planning documents are complete (sprint.md, use cases, architecture)
- [ ] Architecture review passed
- [ ] Stakeholder has approved the sprint plan

## Tickets

| # | Title | Depends On |
|---|-------|------------|
| 001 | dotconfig init and config migration | — |
| 002 | App config wiring for dotconfig-generated env-file | 001 |
| 003 | Dockerfile entrypoint and stack runtime secrets delivery | 002 |
| 004 | Pre-commit audit hook and final dotconfig audit | 003 |

Tickets execute serially in the order listed.
