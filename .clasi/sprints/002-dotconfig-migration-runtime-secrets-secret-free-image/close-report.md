---
sprint: "002"
title: dotconfig migration — runtime secrets, secret-free image
status: closed
---

# Sprint 002 Close Report — dotconfig Migration

## Goal

Retire git-crypt; manage config via dotconfig (SOPS+age); deliver secrets to the
prod container at RUNTIME via a generated env-file instead of baking plaintext
into image layers.

## Tickets (4/4 done)

| # | Title | Key change |
|---|-------|-----------|
| 001 | dotconfig init + config migration | `config/sops.yaml` + `config/{devel,prod,local-prod}/{public,secrets}.env` (SOPS-encrypted); git-crypt removed from `.gitattributes`; `config/secrets/*` untracked + gitignored; `ID_RSA_FILE=id_rsa` declared; SSH key SOPS-encrypted in dotconfig. |
| 002 | App config wiring | `cspawn/util/config.py` `get_config()` now loads a single dotconfig-generated `.env` (via `_find_env_file`, JTL_CONFIG_DIR/JTL_APP_DIR aware), os.environ still wins. `make dev` target. 19 unit tests. |
| 003 | Secret-free image | `docker/Dockerfile` drops all `COPY config/secrets/*` + id_rsa + authorized_keys; new `docker/entrypoint.sh` base64-decodes `$ID_RSA`→`/root/.ssh/id_rsa` at start; `env_file: .env` in stack; `docker/Makefile` `env-file` target, `up: networks env-file`. authorized_keys (inbound SSH) dropped per stakeholder. |
| 004 | Audit hook + final audit | `dotconfig install-hooks` pre-commit audit; `dotconfig audit` == 0; block-test confirmed; CLAUDE.md operator docs. |

## Verification done

- `dotconfig audit` exits 0 (no unencrypted secrets tracked).
- git-crypt fully removed (`.gitattributes` clean).
- `dotconfig load -d prod --no-export -e` produces a complete env with all secret
  keys + `ID_RSA=<base64>` that decodes to a valid OpenSSH private key.
- App loads config via the single `.env`; os.environ precedence preserved.
- Local `docker build` (orbstack): `docker history` shows no secrets;
  `/app/config/secrets` absent; `/root/.ssh` empty in the image.
- 19/19 config unit tests pass (Postgres-dependent tests fail only on local DB
  connection — pre-existing baseline).
- Pre-commit hook blocks a staged dummy secret.

## NOT yet verified (requires prod — deliberately deferred)

The full prod chain has NOT been run end-to-end: `make env-file` (dotconfig load
-d prod) → `make build` on swarm1 → `make up` → container entrypoint decodes
ID_RSA → app SSHes to worker nodes to spawn a host. This must be done at the next
prod deploy, with the rollback image (`pre-dotconfig-migration` tag + the current
running `codeserver:latest`) kept until a real host-start succeeds.

## Notes

- Found + fixed a dotconfig bug: `install-hooks` assumed `.git` is a directory,
  but THIS REPO IS A GIT SUBMODULE of code-server-mono (`.git` is a gitdir
  pointer file). Fix applied in the dotconfig source (`hooks.py` now uses
  `git rev-parse --git-dir`).
- `DO_CLOUD_INIT_FILE` renamed to `DO_CLOUD_INIT` (node.py reads both) so
  dotconfig's bare `-e` doesn't try to embed the cloud-init YAML.
- `pre-dotconfig-migration` git tag marks the rollback point.

## Related

- [[purge-secrets-from-git-history]] — moot/rescope: secrets were git-crypt
  encrypted (never plaintext-leaked); now superseded by dotconfig/SOPS.
