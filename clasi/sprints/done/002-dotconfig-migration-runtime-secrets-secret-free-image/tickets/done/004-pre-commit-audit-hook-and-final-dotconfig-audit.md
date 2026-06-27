---
id: '004'
title: Pre-commit audit hook and final dotconfig audit
status: done
use-cases:
- SUC-004
depends-on:
- '003'
github-issue: ''
issue: dotconfig-migration.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Pre-commit audit hook and final dotconfig audit

## Description

Install the dotconfig pre-commit audit hook and run a comprehensive final audit
to confirm that the entire repository is free of unencrypted secrets. This is
the acceptance gate for the full migration: all prior tickets must be complete
and verified before this ticket is executed.

The `completes_issue: true` setting means completing this ticket will archive
the `dotconfig-migration.md` issue as done.

## Acceptance Criteria

- [x] `dotconfig install-hooks` has been run; `.git/hooks/pre-commit` exists
      and contains the dotconfig audit hook.
      Note: this repo is a git submodule; the hook was installed at
      `.git/modules/code-server-spawner/hooks/pre-commit` (resolved via
      `git rev-parse --git-dir`). A bug in dotconfig's hooks.py was patched
      to handle submodule checkouts where `.git` is a file, not a directory.
- [x] `dotconfig audit` exits 0 with no unencrypted secrets reported anywhere
      in the repository. Output: "No unencrypted secrets detected".
- [x] Manual hook test: staged `config/test-secret.env` containing
      `PASSWORD=hunter2`; `git commit` was blocked with:
      "UNENCRYPTED SECRETS DETECTED — config/test-secret.env:1 PASSWORD
      (key name matches 'password')". Test file removed after verification.
- [x] After unstaging the test file, `dotconfig audit` returned exit 0.
      A clean commit of planning artifacts passed the hook without errors.
- [x] `uv run pytest` result: 19 passed, 4 errors. The 4 errors are all
      Postgres-connection failures (no local Postgres running) in
      test_admin_coverage.py — identical to pre-sprint baseline. All 19
      config/unit tests in test_config.py passed. No regressions.
- [x] The sprint's critical acceptance criteria are all satisfied:
      - [x] git-crypt fully removed: `grep -r git-crypt .gitattributes` is
            empty; `dotconfig audit` exits 0.
      - [x] Prod image contains no secrets in any layer — verified in ticket
            003: ID_RSA is injected at runtime via entrypoint from
            CSPAWN_ID_RSA env var, never baked into the image.
      - [x] App loads correctly in devel — dotconfig cascade in place from
            ticket 002; `dotconfig load -d devel -o .env` + init_app verified.
      - [x] SSH key decoded correctly by entrypoint — verified in ticket 003.
      - [x] Rollback path: old image tag exists and is retrievable per
            ticket 003 verification.

## Implementation Plan

### Approach

1. Run `dotconfig install-hooks` from the project root. This writes
   `.git/hooks/pre-commit` (or appends to it if one already exists).

2. Run `dotconfig audit` across the whole repo and address any remaining
   findings. Common sources:
   - Any plaintext files in `config/` that weren't caught in ticket 001.
   - Any `.env` file accidentally staged (should be in `.gitignore` from
     ticket 001).
   - Any test fixtures or documentation that copied in secret values verbatim.

3. Perform the manual hook test (see Acceptance Criteria above). Confirm the
   hook fires and blocks the commit.

4. Perform the final sprint sign-off checklist (confirm all acceptance criteria
   across tickets 001-003 hold simultaneously in the current state).

5. Run `uv run pytest` to confirm no regressions.

### Files to Modify

- `.git/hooks/pre-commit` — created/updated by `dotconfig install-hooks`
  (this file is not tracked by git; it is local to each checkout)

### Files That May Need Cleanup

- Any remaining plaintext files detected by `dotconfig audit` that escaped
  earlier tickets. Address each finding individually before signing off.

### Testing Plan

- `dotconfig audit` — must exit 0.
- Manual hook block test (described in Acceptance Criteria).
- `uv run pytest` — must pass.
- End-to-end deploy smoke test confirmation (if not already verified in
  ticket 003): `make redeploy` → service starts → login works → host-start
  works.

### Rollback Note

No rollback risk in this ticket: installing the audit hook only adds a
safety gate. If the hook causes issues (e.g., false positives), it can be
temporarily disabled via `git commit --no-verify` while the false positive is
investigated, but this should not be necessary if tickets 001-003 are clean.

### Documentation Updates

Add a comment to the project's `CLAUDE.md` (or a `docs/ops/secrets.md` if
one exists) noting:
- Run `dotconfig install-hooks` after cloning the repo.
- Run `dotconfig load -d <deploy> [--no-export] [-e] -o .env` before starting
  the app locally or deploying.
- Age secret key distribution: operators must obtain the age secret key
  out-of-band (e.g., from 1Password or a secure handoff) before running
  `dotconfig load`.
