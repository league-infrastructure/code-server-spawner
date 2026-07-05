---
id: '001'
title: Ship cloud-init templates in the image; fail loudly on configured-but-missing
  user-data
status: done
use-cases:
- SUC-001
- SUC-002
depends-on: []
github-issue: ''
issue: container-node-expand-missing-cloud-init.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Ship cloud-init templates in the image; fail loudly on configured-but-missing user-data

## Description

Nodes created from *inside* the spawner container (autoscaler scale-up, or
`cspawnctl node expand` run in-container) get no cloud-init user-data,
because `docker/Dockerfile` never copies `config/cloud-init/` into the
image. `_create_droplet` (`cspawn/cli/node.py:1171-1188`) resolves the
configured cloud-init file at `find_parent_dir()/config/cloud-init/<DO_CLOUD_INIT>`;
in-container this resolves to `/app/config/cloud-init/swarm-node-init-v2.yaml`,
which doesn't exist, so the code today logs a warning and proceeds to
create a bare, unprovisioned droplet. `DO_CLOUD_INIT=swarm-node-init-v2.yaml`
is set in every deployment's `public.env` (`config/devel/public.env:21`,
`config/local-prod/public.env:24`, `config/prod/public.env:21`), so this is
the live path in production, not a hypothetical — confirmed on prod
2026-07-05 (`swarm3`: factory `ufw limit 22/tcp` still in place causing
sshd to flap under the spawner's SSH load, docker-ce unpinned at 29.6.0
instead of 29.6.1, no sshd/UFW tuning applied).

This ticket closes both halves of the gap:

1. **Ship the file**: add `config/cloud-init/` (plain YAML only, no
   secrets) to the built image, at the exact path `_create_droplet`
   resolves.
2. **Fail loudly**: when `DO_CLOUD_INIT`/`DO_CLOUD_INIT_FILE` is configured
   but the resolved file is missing or unreadable, abort node creation with
   a clear `click.ClickException` *before* any DigitalOcean side effect
   (SSH-key upload, `droplet.create()`) — a node without its provisioning
   is worse than no node. When `DO_CLOUD_INIT` is unset entirely, behavior
   is unchanged (explicit operator opt-out to proceed without cloud-init).

See `clasi/issues/container-node-expand-missing-cloud-init.md` for the full
root-cause diagnosis, and this sprint's `architecture-update.md` Step 5/6
for the detailed design and rationale (in particular, why the resolution
moves *before* `_ensure_priv_key()`/`_collect_do_ssh_keys()` rather than
just raising in place).

## Acceptance Criteria

- [x] `docker/Dockerfile` adds `COPY config/cloud-init /app/config/cloud-init`
  (placed near the existing `COPY docker/gunicorn_config.py
  /app/config/gunicorn_config.py` at `docker/Dockerfile:70`, which already
  creates `/app/config/`). Only `config/cloud-init/` is copied — the rest
  of `config/` (SOPS-encrypted `secrets.env`, `id_rsa`, `dotconfig.yaml`,
  `sops.yaml`, `known_hosts`, `host-scripts/`) is **not** copied; the image
  stays secret-free per sprint 002.
- [x] `docker/Dockerfile` adds a `RUN` step immediately after the `COPY`
  that fails the build if `/app/config/cloud-init/*.yaml` doesn't exist
  (e.g. `RUN ls /app/config/cloud-init/*.yaml`). This is exercised
  automatically by the existing CI workflow
  (`.github/workflows/docker-publish.yml`), which already runs `docker
  build .` on every PR to `master` — no new CI step needed.
- [x] `.dockerignore` verified to have no rule that excludes `config/` or
  `config/cloud-init/` (confirmed during planning that it doesn't — this
  criterion is a regression guard, not an expected change).
- [x] New `_resolve_cloud_init_path(cfg) -> Path | None` added to
  `cspawn/cli/node.py` (near `_create_droplet`, `cli/node.py:1119`):
  returns `None` when `cfg.get("DO_CLOUD_INIT")` and
  `cfg.get("DO_CLOUD_INIT_FILE")` are both falsy; otherwise returns
  `Path(find_parent_dir()) / "config" / "cloud-init" / <configured file>`
  (no existence check — that's the caller's job).
- [x] `_create_droplet` (`cspawn/cli/node.py:1119-1256`) refactored: the
  cloud-init resolution block moves to the very start of the `else`
  (not-`existing`) branch (`cli/node.py:1166` today), *before*
  `_ensure_priv_key()`/`_collect_do_ssh_keys()` are called, so a failed
  resolution produces zero DigitalOcean API calls (no SSH key uploaded, no
  droplet created).
- [x] Configured + file present: unchanged behavior — file contents read
  and passed as `user_data=` to `digitalocean.Droplet(...)`.
- [x] Configured + file missing or unreadable: raises
  `click.ClickException` whose message includes the resolved path that was
  checked and a remediation hint (fix the path/config, or unset
  `DO_CLOUD_INIT` to proceed without cloud-init).
- [x] Unset `DO_CLOUD_INIT`/`DO_CLOUD_INIT_FILE`: unchanged behavior —
  proceeds with `user_data=None`, logs an informational message, no
  exception.
- [x] Unit test: configured + missing file → `click.ClickException` raised;
  mocked `digitalocean.Droplet`/`.create()` never called.
- [x] Unit test: configured + found file → file content passed through as
  the `user_data=` kwarg to the mocked `digitalocean.Droplet(...)` call.
- [x] Unit test: unset config → proceeds with `user_data=None`, no
  exception (regression guard for the explicit opt-out case).

## Implementation Plan

**Approach**: Minimal-diff refactor of the existing try/except block
inside `_create_droplet`'s `else` branch. Extract the path-resolution logic
into `_resolve_cloud_init_path(cfg)` so it can be reused unmodified by
ticket 002's `_expected_docker_version()`. Replace the
`log.warning(...); proceeding` branch with `raise click.ClickException(...)`.
Reorder so this resolution happens before any DigitalOcean side effect.

**Files to create/modify**:
- `docker/Dockerfile` — add `COPY` + `RUN` self-check.
- `cspawn/cli/node.py` — add `_resolve_cloud_init_path`; refactor
  `_create_droplet`'s cloud-init block.
- New test file: `test/test_node_cloud_init.py`.

**Testing plan**:
- Follow `test/test_node_unpin.py` / `test/test_node_contract.py`
  MagicMock/`patch()` conventions: `patch("cspawn.cli.node.get_config",
  return_value={...})`, `patch("digitalocean.Droplet")`,
  `patch("digitalocean.Manager")` as needed.
- For path resolution, use a real `tmp_path` as the project root (patch
  `cspawn.cli.node.find_parent_dir` to return `tmp_path`, or set
  `JTL_APP_DIR` env var) rather than mocking `Path.exists`/`Path.read_text`
  — matches `test/test_config.py`'s `tmp_path` style and is less brittle.
- Run `uv run pytest test/test_node_cloud_init.py -v` plus the full suite
  to confirm no regression in `test_node_contract.py`/`test_node_unpin.py`
  (both exercise `cli.node.get_config` patching patterns this ticket
  touches indirectly).
- Dockerfile change is verified by the existing CI `docker build` step —
  no local Docker build required for this ticket's own test pass, but
  worth a manual `docker build .` sanity check before marking done if
  Docker is available locally.

**Documentation updates**: Update `_create_droplet`'s docstring to
describe the new fail-loud contract for `DO_CLOUD_INIT`.

## Testing

- **Existing tests to run**: `uv run pytest test/test_node_contract.py
  test/test_node_unpin.py test/test_config.py`
- **New tests to write**: `test/test_node_cloud_init.py` — see Acceptance
  Criteria and Implementation Plan above.
- **Verification command**: `uv run pytest`
