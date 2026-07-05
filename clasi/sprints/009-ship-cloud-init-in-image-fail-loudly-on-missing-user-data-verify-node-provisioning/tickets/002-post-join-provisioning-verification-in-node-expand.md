---
id: '002'
title: Post-join provisioning verification in node expand
status: done
use-cases:
- SUC-003
depends-on:
- '001'
github-issue: ''
issue: container-node-expand-missing-cloud-init.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Post-join provisioning verification in node expand

## Description

Even with ticket 001 shipping the cloud-init file and failing loudly on a
missing configuration, cloud-init can still fail to apply correctly
*after* a node boots (a transient SSH/UFW race, an apt failure mid-run).
`_wait_for_cloud_init` (`cspawn/cli/node.py:510-553`) already waits for
cloud-init to report done, but it is explicitly best-effort — it logs and
proceeds on timeout or an unclear status, by design, so a slow-but-fine
node isn't blocked forever. Nothing today hard-fails when cloud-init
genuinely never finishes correctly, so a defective node can join the swarm
and silently start receiving code-server hosts.

This ticket adds a separate, later, hard-fail verification gate: after a
node joins the swarm via `cspawnctl node expand`, verify over SSH that (a)
several consecutive connects succeed, (b) `docker --version` matches the
version pinned in the configured cloud-init file, and (c) `cloud-init
status` reports done. On failure, drain the node (so Swarm's scheduler
stops considering it) and abort the CLI command with a non-zero exit —
per the issue's acceptance criteria, "a defective node can never silently
receive hosts."

Depends on ticket 001: `_expected_docker_version()` relies on
`_resolve_cloud_init_path()` (added in 001) to locate the cloud-init file
whose `DOCKER_PIN` it parses.

See `clasi/issues/container-node-expand-missing-cloud-init.md` and this
sprint's `architecture-update.md` Step 5/6 (in particular the rationale for
parsing the expected docker version from the cloud-init file instead of a
new config key, returning a failure list instead of raising directly from
the verification function, and draining rather than destroying a failed
node).

## Acceptance Criteria

- [x] New `_expected_docker_version(cfg) -> str | None` added to
  `cspawn/cli/node.py` (near `_wait_for_cloud_init`, `cli/node.py:510`):
  resolves the configured cloud-init file via `_resolve_cloud_init_path`
  (ticket 001), regex-parses the `DOCKER_PIN="5:X.Y.Z-..."` pattern already
  documented in `config/cloud-init/swarm-node-init-v2.yaml:88-105`, returns
  the version string (e.g. `"29.6.1"`). Returns `None` if unconfigured, the
  file can't be read, or the pattern isn't found — callers treat `None` as
  "skip the version check," never as an error.
- [x] New `_verify_node_provisioning(ip, key_path, *, expected_docker_version,
  ssh_checks=3, retry_delay=2.0, log=None) -> list[str]` added to
  `cspawn/cli/node.py`: returns a list of human-readable failure strings
  (empty list = healthy). Never raises for an expected failure mode (SSH
  down, version mismatch, cloud-init not done) — only truly unexpected
  errors (e.g. an invalid key file) may propagate.
  - [x] Check (a): performs `ssh_checks` consecutive SSH connect attempts
    (reusing `_ssh_exec`, `cli/node.py:556`) with `retry_delay` seconds
    between attempts; if fewer than `ssh_checks` succeed, appends a failure
    string naming the count (e.g. `"SSH reachability: 2/3 consecutive
    connects succeeded"`).
  - [x] Check (b): runs `docker --version` over SSH; if
    `expected_docker_version` is not `None` and is not a substring of the
    output, appends a failure string naming expected vs. actual. Skipped
    entirely when `expected_docker_version is None`.
  - [x] Check (c): runs `cloud-init status` over SSH; if the output doesn't
    contain `"status: done"`, appends a failure string with the actual
    status text.
- [x] `expand()` (`cspawn/cli/node.py`, the CLI command spanning
  `cli/node.py:2215-2367`): immediately after the existing "verify node
  appears in swarm membership" block (`cli/node.py:2336-2350`), guarded by
  `last_ip and last_shortname` being known (i.e., this invocation ran
  configure+join), calls `_verify_node_provisioning` with
  `expected_docker_version=_expected_docker_version(cfg)` and a fresh
  `_ensure_priv_key()` key path.
- [x] On verification failure: `log.error(...)` with the full failure list;
  best-effort look up the node via `_find_swarm_node` (`cli/node.py:797`)
  and drain it via `_drain_swarm_node` (`cli/node.py:848`) — a drain
  failure itself is caught and logged, never raised; then raise
  `click.ClickException` summarizing the failures and stating the node was
  drained (non-zero CLI exit).
- [x] On verification success: log an info line confirming the node passed;
  existing summary output (`"Created and joined node: ..."`) and the
  trailing `_sync_domain_records` call are unchanged.
- [x] Unit tests (mock `cspawn.cli.node._ssh_exec` directly — no real
  paramiko/network — following the mocking depth already used elsewhere in
  this file's test suite):
  - [x] All three checks pass → `_verify_node_provisioning` returns `[]`.
  - [x] SSH flaky (some but not all of `ssh_checks` attempts succeed) →
    returned list contains a string mentioning the success count.
  - [x] `docker --version` output doesn't contain `expected_docker_version`
    → returned list contains a mismatch string with both values.
  - [x] `cloud-init status` output lacks `"status: done"` → returned list
    contains a string with the actual status text.
  - [x] `expected_docker_version=None` → version check is skipped, no
    false failure appended.
- [x] CliRunner test: `expand` with a mocked `_verify_node_provisioning`
  failure exits non-zero (`result.exit_code != 0`) and drain is attempted
  (assert `_find_swarm_node`/`_drain_swarm_node` — or the docker
  node-lookup+update call chain — was invoked).
- [x] CliRunner regression test: `expand` with verification success exits 0
  and prints the existing "Created and joined node" summary, unchanged
  from pre-ticket behavior.

## Implementation Plan

**Approach**: Add the two new helper functions as logical neighbors of
`_wait_for_cloud_init` (same file, same conceptual area: post-boot node
health). Wire a single verification block into `expand()` right after the
existing membership-wait loop. Keep `_verify_node_provisioning` itself free
of `cfg`-parsing concerns (it takes an already-resolved
`expected_docker_version`) so it stays a small, pure-ish, easily-mockable
unit — the `cfg`-to-version resolution lives entirely in
`_expected_docker_version`.

**Files to create/modify**:
- `cspawn/cli/node.py` — add `_expected_docker_version`,
  `_verify_node_provisioning`; wire into `expand()`.
- New test file: `test/test_node_provisioning_verify.py`.

**Testing plan**:
- Patch `cspawn.cli.node._ssh_exec` to return controlled `(exit_code, out,
  err)` tuples per call, simulating each check's pass/fail combinations.
- For the CliRunner tests, reuse the `get_config`/`get_app` mocking
  scaffold already established in `test/test_node_op_cli.py`
  (`patch("cspawn.cli.node.get_config", return_value={...})`) plus mocks
  for `digitalocean.Manager`, `docker.DockerClient`, `_create_droplet`,
  `_configure_node`, `_join_swarm` so `expand()` can run end-to-end in a
  test without real infrastructure.
- Run `uv run pytest test/test_node_provisioning_verify.py -v` plus the
  full suite (`uv run pytest`) to confirm no regression to `expand()`'s
  existing CLI tests.

**Documentation updates**: Docstring on `_verify_node_provisioning`
explaining the three checks and the best-effort-per-check /
hard-fail-in-aggregate split. No user-facing docs changes required.

## Testing

- **Existing tests to run**: `uv run pytest test/test_node_contract.py
  test/test_node_unpin.py test/test_node_cloud_init.py` (ticket 001's new
  suite must still pass — this ticket builds on its helpers)
- **New tests to write**: `test/test_node_provisioning_verify.py` — see
  Acceptance Criteria and Implementation Plan above.
- **Verification command**: `uv run pytest`
