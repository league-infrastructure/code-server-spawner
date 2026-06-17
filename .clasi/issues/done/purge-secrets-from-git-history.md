---
status: done
---

# Purge committed secrets from git history and prevent re-commit

## RESOLUTION (closed as moot — 2026-06-16)

This issue was based on a mistaken premise. The secrets were **git-crypt
encrypted** in git / on GitHub all along (`.gitattributes`:
`**/secrets/* filter=git-crypt`) — they were NEVER committed or pushed as
plaintext. The plaintext seen locally is git-crypt's decrypted working-tree view.
So a history purge is not a security necessity.

Superseded by **sprint 002 (dotconfig-migration)**, which removed git-crypt
entirely and moved secrets to dotconfig/SOPS with runtime-only delivery (no
secrets in image layers). `config/secrets/` is now untracked + gitignored and a
`dotconfig audit` pre-commit hook prevents future unencrypted commits.

Residual (low urgency): the creds were briefly decrypted on a local machine
during this session; rotate at leisure if desired (GitHub + DO tokens already
rotated). Closing.

## Context

The repo commits secrets in plaintext. Tracked secret files include
`config/secrets/{prod,local-prod,devel,docker,secret}.env`,
`config/secrets/id_rsa` (a **private SSH key**), `config/secrets/id_rsa.pub`,
and two Google `client_secret_*.json` files. Secrets appear in ~13 commits
across history. Remote is GitHub `league-infrastructure/code-server-spawner`
(`master` + a `copilot/*` branch + `origin/HEAD`).

This must land on a clean `master` **after sprint 001 merges**, and **before**
the dotconfig migration sprint.

## Scope

1. **Rotate the exposed credentials FIRST** — a history rewrite does not
   un-leak anything already pushed; every committed secret is compromised:
   - `config/secrets/id_rsa` SSH keypair — regenerate, update
     `authorized_keys` on all swarm droplets (swarm1/2/3) and DO SSH keys.
   - `DO_TOKEN`, `GITHUB_TOKEN`, `GITHUB_ORG_TOKEN`.
   - Google OAuth client secrets (both clients).
   - `SECRET_KEY`, `ENCRYPTION_KEY`, Postgres creds.
   - Produce a rotation checklist and work through it.

2. **Purge the secret files from ALL git history** using `git-filter-repo`
   (`/Users/eric/.pyenv/shims/git-filter-repo`). Rewrites history →
   coordinate force-push to `origin/master`, handle the `copilot/*` branch;
   anyone with a clone must re-clone.

3. **Prevent re-commit** — add `config/secrets/` (and `*.env` secret files,
   `id_rsa*`) to `.gitignore`. Consider `dotconfig install-hooks` (the audit
   pre-commit hook) as belt-and-suspenders.

## Acceptance

- Secret files absent from all history
  (`git log --all -- config/secrets/prod.env` returns nothing).
- `config/secrets/` gitignored; secrets can't be re-committed.
- Force-push completed; `copilot/*` branch handled.
- Rotation checklist complete — all creds rotated and verified.
- CI / Codespaces / deploys still work with rotated creds.

## Risk

Destructive, outward-facing (force-push to a shared GitHub repo).
Requires explicit stakeholder go-ahead at execution time.
