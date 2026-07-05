---
id: 009
title: Ship cloud-init in image, fail loudly on missing user-data, verify node provisioning
status: done
branch: sprint/009-ship-cloud-init-in-image-fail-loudly-on-missing-user-data-verify-node-provisioning
use-cases:
- SUC-001
- SUC-002
- SUC-003
issues:
- container-node-expand-missing-cloud-init.md
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 009: Ship cloud-init in image, fail loudly on missing user-data, verify node provisioning

## Goals

Close the upstream cause behind nodes that silently boot without their
provisioning script when created from inside the spawner container
(autoscaler scale-up, or `cspawnctl node expand` run in-container): the
Docker image never contained `config/cloud-init/`, so the configured
cloud-init file resolves to a path that doesn't exist, and `_create_droplet`
today treats that as a warning and proceeds anyway. Three changes close the
loop:

1. Ship the (secret-free) cloud-init templates inside the image.
2. Fail loudly — abort node creation — if `DO_CLOUD_INIT` is configured but
   the resolved file is missing, instead of silently creating an
   unprovisioned droplet.
3. Add a post-join provisioning verification gate (SSH reachability, docker
   version pin, cloud-init completion) to both `node expand` and the
   autoscaler's scale-up path, so a node that joined the swarm without being
   correctly provisioned can never silently start receiving hosts.

## Problem

`_create_droplet` (`cspawn/cli/node.py:1171-1188`) resolves the cloud-init
file as `find_parent_dir()/config/cloud-init/<DO_CLOUD_INIT>`. Inside the
container, `docker/Dockerfile` copies `cspawn/`, `migrations/`, `data/`, and
a few individual `docker/` files, but never `config/` — deliberately, since
`config/` also holds SOPS-encrypted secrets and the image must stay
secret-free (sprint 002). `DO_CLOUD_INIT=swarm-node-init-v2.yaml` is set in
every deployment's `public.env` (`devel`, `local-prod`, `prod`), so this path
is live in production, not hypothetical: the file never resolves inside the
container, the code logs a warning and proceeds, and DigitalOcean creates a
bare `docker-20-04` marketplace droplet with:

- the factory `ufw limit 22/tcp` rule intact, which throttles the spawner's
  own SSH-over-Docker traffic until sshd flaps and every code host on the
  node sticks at "starting";
- no docker-ce version pin, so the node can join with a docker-ce version
  that mismatches the swarm manager's;
- none of the sshd `MaxStartups` tuning or swarm-dataplane UFW rules that
  `config/cloud-init/swarm-node-init-v2.yaml` provides.

Observed live on prod 2026-07-05: hosts `bilkton` and `eric-busboom` stuck
"starting" on node `swarm3`; `swarm3` refused SSH until rebooted; docker
29.6.0 instead of the pinned 29.6.1. See
`clasi/issues/container-node-expand-missing-cloud-init.md` for the full
diagnosis.

## Solution

- **Dockerfile**: copy only `config/cloud-init/` (plain YAML, no secrets)
  into the image, alongside a build-time self-check that fails `docker
  build` if the directory ends up empty.
- **Fail loudly**: `_create_droplet` aborts with `click.ClickException`
  *before* any droplet-creation side effect (SSH-key upload, `droplet.create()`)
  when `DO_CLOUD_INIT`/`DO_CLOUD_INIT_FILE` is configured but the resolved
  file is missing or unreadable. When it is unset entirely, behavior is
  unchanged (explicit operator opt-out to proceed without cloud-init).
- **Post-join verification**: a new `_verify_node_provisioning()` helper
  checks, over SSH, immediately after a node joins the swarm: (a) several
  consecutive SSH connects succeed, (b) `docker --version` matches the
  version pinned in the configured cloud-init file (parsed via regex — no
  new config key, single source of truth stays the cloud-init YAML), (c)
  `cloud-init status` reports `done`. On failure, the node is drained
  (best-effort, via the existing `_find_swarm_node`/`_drain_swarm_node`
  primitives) so it can never be scheduled, and:
  - `node expand` logs `ERROR` and exits non-zero (`click.ClickException`).
  - the autoscaler's scale-up path (`cspawn/cs_docker/autoscale.py::apply_plan`)
    logs `ERROR`, records the failure in `ApplyResult.errors`, does **not**
    count the node toward `result.added`, and continues to the next planned
    node rather than aborting the whole batch.

## Success Criteria

- A `node expand` (or autoscaler scale-up) run from inside the container
  produces a droplet whose cloud-init has actually executed (UFW rule
  fixed, docker pinned, sshd tuned) — verifiable by the new post-join checks
  passing.
- A misconfigured/missing cloud-init file aborts node creation instead of
  silently producing a bare droplet.
- A node that joins the swarm without being correctly provisioned is
  drained and never assigned hosts, and the failure is loudly logged in
  both the manual and automated (autoscaler) provisioning paths.

## Scope

### In Scope

- `docker/Dockerfile`: `COPY config/cloud-init /app/config/cloud-init` +
  build-time existence check.
- `cspawn/cli/node.py::_create_droplet`: fail-loud on configured-but-missing
  cloud-init file; new `_resolve_cloud_init_path()` helper.
- `cspawn/cli/node.py`: new `_expected_docker_version()` and
  `_verify_node_provisioning()` helpers; wired into `expand()`'s post-join
  step.
- `cspawn/cs_docker/autoscale.py::apply_plan`: same verification wired into
  the scale-up loop, reusing the node.py helpers.
- Unit tests for all of the above, following existing MagicMock/CliRunner
  conventions (`test/test_node_unpin.py`, `test/test_node_contract.py`,
  `test/test_autoscale.py`).

### Out of Scope

- **Domain-record sync upsert** (originally flagged as a possible item 4):
  verified live during sprint planning (2026-07-05) that
  `_sync_domain_records` (`cspawn/cli/node.py:1792`) already updates an
  existing A record's stale IP on the next `node expand` run (confirmed by
  the operator's own live run: `[domains] Updated A swarm3.dojtl.net ->
  64.23.130.10 (ttl=60s)`). The earlier stale-DNS observation was a timing
  artifact (the sync step runs last in the `expand` flow and simply hadn't
  executed yet), not a bug. **No ticket for this item.**
- **`os.environ`-over-`.env` `DO_TOKEN` shadowing** (`cspawn/util/config.py:174-177`):
  a stale exported `DO_TOKEN` in an operator's shell silently overrides the
  correct dotconfig value. Real trap, but orthogonal to this sprint's node
  provisioning fix. Deferred — may become its own issue/sprint if it recurs.
- Any change to the docker-ce version pin mechanism itself (still a
  hand-maintained comment in `config/cloud-init/swarm-node-init-v2.yaml`);
  this sprint only reads that pin for verification, it does not change how
  or where it's declared.
- Automated remediation of a failed/drained node (e.g. auto-destroy, retry).
  A drained node is left for manual operator investigation.

## Test Strategy

Unit tests only, no live DigitalOcean/Docker/SSH access, following this
repo's established conventions:

- **CliRunner + MagicMock** for CLI-level behavior (`test_node_unpin.py`,
  `test_node_contract.py::TestContractNodeCLI` patterns): patch
  `cspawn.cli.node.get_config`, `digitalocean.Manager`/`digitalocean.Droplet`,
  and `docker.DockerClient` to drive `_create_droplet`/`expand()` without any
  network access.
- **Path-resolution tests** for the cloud-init fail-loud logic use a real
  temp directory as the resolved project root (patching
  `cspawn.cli.node.find_parent_dir` or setting `JTL_APP_DIR`) rather than
  mocking the filesystem, matching `test_config.py`'s `tmp_path` style —
  simpler and less brittle than mocking `Path.exists`/`Path.read_text`.
- **SSH-level tests** for `_verify_node_provisioning` patch
  `cspawn.cli.node._ssh_exec` directly (no real paramiko/network), following
  the level of mocking already used for `_ssh_exec_retry` callers.
- **Autoscaler tests** extend `test_autoscale.py::TestApplyPlan`, patching
  `cspawn.cli.node._create_droplet` / `_configure_node` / `_join_swarm` /
  `_verify_node_provisioning` by their `cspawn.cli.node.*` dotted path (the
  functions are imported locally inside `apply_plan`, so patches must target
  the definition module, matching the existing pattern at
  `test_autoscale.py:1024`).
- **Dockerfile**: enforced by the build-time `RUN` self-check plus the
  existing CI workflow (`.github/workflows/docker-publish.yml`), which
  already runs `docker build` on every PR to `master` — no new CI step
  needed.

## Architecture Notes

See `architecture-update.md` for the full design. Key constraint: the image
must remain secret-free (sprint 002) — only `config/cloud-init/` (plain
YAML) is copied, never the rest of `config/` (which holds SOPS-encrypted
`secrets.env` and `id_rsa`).

## GitHub Issues

(None linked yet.)

## Definition of Ready

Before tickets can be created, all of the following must be true:

- [x] Sprint planning documents are complete (sprint.md, use cases, architecture)
- [x] Architecture review passed
- [x] Stakeholder has approved the sprint plan

## Tickets

| # | Title | Depends On |
|---|-------|------------|
| 001 | Ship cloud-init templates in the image; fail loudly on configured-but-missing user-data | — |
| 002 | Post-join provisioning verification in `node expand` | 001 |
| 003 | Wire post-join provisioning verification into the autoscaler's scale-up path | 002 |

Tickets execute serially in the order listed.
