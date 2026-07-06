---
id: '001'
title: Post-join verify compares docker version by major only, reusing _major
status: done
use-cases:
- SUC-001
depends-on: []
github-issue: ''
issue: node-provisioning-major-version-verify-and-race-proof-docker-install.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Post-join verify compares docker version by major only, reusing _major

## Description

`_verify_node_provisioning`'s docker-version check (`cspawn/cli/node.py:585-654`,
comparison at ~628-640) fails a node unless the *exact* pinned version
string (e.g. `29.6.1`) is a substring of `docker --version`'s output. Docker
Swarm join/operation only require **major**-version compatibility between
manager and workers — the standard `_join_swarm`'s own pre-join preflight
(`cli/node.py:1564-1604`) already applies, via a private `_major(v) ->
int | None` closure defined inline in that function. This mismatch in
strictness is exactly what drained a healthy `swarm5` worker (`29.6.0`) under
a `29.6.1` manager on 2026-07-06, starting the fleet-wide-disconnect
incident described in
`clasi/issues/node-provisioning-major-version-verify-and-race-proof-docker-install.md`.

This ticket:
1. Promotes `_major()` from a closure private to `_join_swarm` to a
   module-level function in `cspawn/cli/node.py`, so there is exactly one
   definition of "what is this docker version's major number."
2. Points `_join_swarm`'s existing preflight at the module-level `_major()`
   (no behavior change there — it already only blocks on a major mismatch).
3. Rewrites `_verify_node_provisioning`'s docker-version check to compare
   `_major(expected_docker_version)` against `_major(actual_version_output)`
   instead of doing an exact-substring test.

`_expected_docker_version()` is unchanged — it still returns the pinned
`X.Y.Z` string; only how `_verify_node_provisioning` *compares* it changes.
`expected_docker_version is None` continues to skip the check entirely.

See `architecture-update.md` Step 5 ("What Changed" — M1) and Step 6
(design rationale for reusing `_major()` rather than a second parsing
definition, and for leaving `_expected_docker_version`'s return type
unchanged).

## Acceptance Criteria

- [x] `_major(v: str | None) -> int | None` is promoted to module scope in
  `cspawn/cli/node.py` (placed alongside `_DOCKER_PIN_RE`/
  `_expected_docker_version`/`_verify_node_provisioning`), with identical
  behavior to today's closure: regex-match a leading integer; return `None`
  on no match, a falsy input, or any exception.

  Note: the promoted `_major` extends the closure's `^(\d+)` anchor to
  `re.search(r"(\d+)\.\d+\.\d+", ...)` — matching the leading integer of the
  first `X.Y.Z`-shaped version number *anywhere* in the string, not only at
  offset 0. This was required to make `_major` actually usable on
  `_verify_node_provisioning`'s free-form `docker --version` output (e.g.
  `"Docker version 29.6.0, build fb59821"`), which never starts with a
  digit; a strict `^(\d+)` anchor would make `actual_major` always `None`
  and the version check would always fail. `_join_swarm`'s existing bare
  `"29.6.1"`-shaped inputs (`manager_client.version()["Version"]` and
  `docker version --format '{{.Server.Version}}'`) match identically under
  both the old and new regex, so `_join_swarm`'s preflight behavior is
  unchanged. Verified by the passing `test_flaky_ssh_reports_success_count`
  / `test_all_checks_pass_returns_empty_list` (realistic `"Docker version
  X.Y.Z, build ..."` fixtures) and the direct `_major()` unit tests below.
- [x] `_join_swarm`'s pre-join preflight (currently `cli/node.py:1564-1604`)
  calls the module-level `_major()` instead of defining its own local
  closure. No behavior change: still raises `click.ClickException` only
  when both majors resolve and differ.
- [x] `_verify_node_provisioning`'s docker-version check (currently
  `cli/node.py:628-640`) replaces `expected_docker_version not in
  docker_version_output` with a comparison of `_major(expected_docker_version)`
  vs. `_major(docker_version_output)`.
  - [x] A node whose docker major matches the expected major but whose
    patch/minor differs (expected `29.6.1`, node reports `Docker version
    29.6.0, build abc123`) now **passes** the check.
  - [x] A node whose docker major genuinely differs (expected `29.6.1`,
    node reports `Docker version 28.4.0, build xyz`) still **fails**, with
    a failure string naming both full version strings (not bare integers),
    so an operator reading the log sees the concrete versions involved.
  - [x] When the actual version's major cannot be parsed (empty/garbled
    `docker --version` output), the check fails — conservative, matching
    today's posture for unparseable output.
  - [x] `expected_docker_version=None` still skips the check entirely
    (regression guard, unchanged from today).
- [x] `_major` is directly importable (`from cspawn.cli.node import
  _major`) and has direct unit test coverage: a normal `"29.6.1"`-shaped
  input, a free-form `"Docker version 29.6.0, build abc"`-shaped input,
  `None` input, and an unparseable/garbage input.
- [x] `test/test_node_provisioning_verify.py` updated:
  - [x] `test_docker_version_mismatch_reports_both_values` (currently
    asserts `29.6.0` vs. expected `29.6.1` is a *failure*) is replaced with
    a genuine major-mismatch case (e.g. expected `29.6.1`, actual
    `28.4.0`) and a new, separate test asserting the patch-only-difference
    case (`29.6.0` vs. expected `29.6.1`) returns `[]`.
  - [x] `test_all_checks_pass_returns_empty_list`,
    `test_flaky_ssh_reports_success_count`,
    `test_cloud_init_not_done_reports_actual_status`,
    `test_expected_version_none_skips_version_check`,
    `test_multiple_failures_all_reported` continue to pass unmodified (their
    fixture docker-version values are already same-major or intentionally
    far-off-major, so their expected outcomes don't change under the new
    logic) — confirmed, not just assumed, by running the suite.
- [x] Docstrings updated: `_verify_node_provisioning`'s docstring (currently
  says "not a substring of the output") describes major-only comparison;
  a short comment above `_major` notes it is shared by `_join_swarm`'s
  preflight and `_verify_node_provisioning` so future edits can't
  reintroduce a second, drifting definition.
- [x] Full suite green: `uv run pytest` (excluding the known pre-existing
  `test_admin_coverage.py` PRODUCTION-env failures).

## Implementation Plan

**Approach**: Move the `_major` closure out of `_join_swarm` verbatim (same
regex, same signature, same `None`-on-failure behavior) to module scope,
near `_DOCKER_PIN_RE`/`_expected_docker_version`/`_verify_node_provisioning`
(this file's existing "version compatibility" neighborhood). Update
`_join_swarm` to reference the module-level name — this is a pure
extraction with no logic change. Then rewrite `_verify_node_provisioning`'s
check (b) body:

```python
if expected_docker_version is not None:
    try:
        _code, out, err = _ssh_exec(ip, "root", key_path, "docker --version")
        docker_version_output = (out or err or "").strip()
    except Exception as e:
        docker_version_output = ""
        if log:
            log.warning(f"[expand] verify: docker --version failed: {e}")
    expected_major = _major(expected_docker_version)
    actual_major = _major(docker_version_output)
    if expected_major is None or actual_major is None or expected_major != actual_major:
        failures.append(
            f"docker version mismatch: expected major from {expected_docker_version!r}, "
            f"got {docker_version_output!r}"
        )
```

(Illustrative — exact wording/formatting of the failure string left to
implementation, but it must include both the expected and actual full
version strings, per the acceptance criteria above.)

**Files to create/modify**:
- `cspawn/cli/node.py` — promote `_major`, update `_join_swarm`'s call
  site, rewrite `_verify_node_provisioning`'s version-check block, update
  docstrings.
- `test/test_node_provisioning_verify.py` — update the mismatch test,
  add a passing-patch-difference test, add direct `_major()` unit tests.

**Testing plan**:
- Unit-test `_major()` directly with a `"29.6.1"`-shaped string, a
  free-form `"Docker version 29.6.0, build abc"`-shaped string, `None`,
  and an unparseable string.
- Extend `TestVerifyNodeProvisioning`: major-match/patch-differs → `[]`;
  genuine major-mismatch → one failure string naming both full version
  values.
- Run `uv run pytest test/test_node_provisioning_verify.py -v`, then the
  full suite.

**Documentation updates**: `_verify_node_provisioning`'s docstring and a
short "shared with `_join_swarm`'s preflight" comment above `_major`, as
noted in Acceptance Criteria.

## Testing

- **Existing tests to run**: `uv run pytest test/test_node_provisioning_verify.py
  test/test_node_cloud_init.py test/test_node_op_cli.py` (sanity-check that
  promoting `_major` to module scope doesn't disturb any other test that
  imports from `cspawn.cli.node`).
- **New tests to write**: direct `_major()` unit tests; a passing
  patch-difference case and a genuine major-mismatch case for
  `_verify_node_provisioning`, as detailed in Acceptance Criteria and the
  Implementation Plan.
- **Verification command**: `uv run pytest`
