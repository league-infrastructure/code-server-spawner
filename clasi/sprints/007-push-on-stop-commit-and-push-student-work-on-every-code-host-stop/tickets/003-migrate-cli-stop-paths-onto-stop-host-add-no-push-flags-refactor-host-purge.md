---
id: '003'
title: Migrate CLI stop paths onto stop_host, add --no-push flags, refactor host purge
status: open
use-cases:
- SUC-005
- SUC-006
- SUC-009
depends-on:
- '001'
github-issue: ''
issue: push-on-host-stop.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Migrate CLI stop paths onto stop_host, add --no-push flags, refactor host purge

## Description

Migrate the four CLI-invoked stop paths — `host stop`, `sys shutdown`,
`host purge`, and the `test teardown` load-test fixture cleanup — onto
`CodeServerManager.stop_host()` (ticket 001). `host stop` and `sys
shutdown` gain a new `--no-push` flag (they never had a push step
before this sprint). `host purge` keeps its existing `--no-push` /
`--dry-run` flags with observably identical output, now implemented via
the shared choke point instead of its own inline block — this is the
ticket that fulfills `push-on-host-stop.md`'s acceptance criterion
"Existing `host purge` push behavior is refactored onto the shared
choke point rather than duplicated." `test teardown` always passes
`push=False` (test-student work is never meaningfully pushed, per the
issue's explicit allowance). This is the last ticket in the sprint —
completing it satisfies every stop path enumerated in the issue.

## Acceptance Criteria

- [ ] `cspawn/cli/host.py` `stop` command (currently lines 111-132)
      gains a `--no-push` flag (`is_flag=True`, default `False`, i.e.
      push happens by default — consistent with every other migrated
      path).
- [ ] `stop <name>`: resolves the `CodeHost` row for `name` via
      `CodeHost.query.filter_by(service_name=...).first()`; if found,
      calls `app.csm.stop_host(ch, push=not no_push)`; if no matching
      `CodeHost` row exists (orphan Swarm service with no DB record),
      falls back to a direct `s.stop()` with a printed/logged warning
      that push was skipped because there is no DB record to push from.
- [ ] `stop --all`: for each live service `s` returned by
      `app.csm.list()`, resolves its `CodeHost` row via the existing
      `CSMService.rec` property; calls `stop_host(ch, push=not no_push)`
      when a row exists, else falls back as above; one host's failure
      does not abort the loop over the rest.
- [ ] `cspawn/cli/sys.py` `shutdown` command (currently lines 13-19)
      gains a `--no-push` flag, passed through as
      `app.csm.remove_all(push=not no_push)`.
- [ ] `cspawn/cli/host.py` `purge` command's (currently lines 187-240)
      inline push/stop/delete block — `CodeHostRepo.new_codehostrepo(...).push()`
      then `app.csm.get(ch)`/`s.stop()` then `db.session.delete(ch)`,
      each in its own try/except — is replaced by a single
      `app.csm.stop_host(ch, push=not no_push)` call per targeted host.
- [ ] `purge`'s existing observable behavior is unchanged: same
      `--no-push` / `--dry-run` flags; same per-host stdout shape
      (`"(pushed)"` / `"(push failed: ...)"` / `"Stopped and deleted:
      <name>"` for the real run; `"Would push, stop and delete: <name>"`
      / `"Would stop and delete: <name>"` for `--dry-run`); same final
      `app.db.session.commit()` timing (only after the loop, only when
      not `--dry-run`).
- [ ] `cspawn/cli/test.py` `teardown` command's (currently lines
      339-412) `s.stop()` call is replaced by
      `app.csm.stop_host(ch, push=False)` when a `CodeHost` row exists
      for the test student being torn down; the existing `--dry-run`
      output (`"would stop service <name>"`) is unchanged.
- [ ] `cspawn/cli/node.py` `rebalance` is verified unchanged (no code
      edit expected) — confirm it inherits the ticket-001 timeout
      hardening automatically since it calls the same
      `CodeHostRepo(...).push()` method.
- [ ] Unit tests (Click `CliRunner`, mocking `app.csm` /
      `CodeServerManager.stop_host`) cover: `--no-push` skips push on
      both `host stop` and `sys shutdown`; `host purge`'s stdout format
      is unchanged before/after the refactor for the pushed / push-failed
      / stop-failed / dry-run cases; `test teardown` never calls push
      regardless of flags.

## Implementation Plan

### Approach

1. `cli/host.py` `stop`: add
   `@click.option("--no-push", is_flag=True, help="Skip pushing each
   host's changes to GitHub before stopping it.")`. For the single-name
   path, look up `ch = CodeHost.query.filter_by(service_name=service_name).first()`
   and branch on `ch` presence as described in Acceptance Criteria. For
   `--all`, iterate `app.csm.list()` and use `s.rec` to get the row per
   service.
2. `cli/host.py` `purge`: delete the inline
   `if not no_push: try: ch_repo = CodeHostRepo.new_codehostrepo(...);
   ch_repo.push() ... except ...` block and the following
   `try: s = app.csm.get(ch); if s: s.stop() ... except ...` block;
   replace both with `result = app.csm.stop_host(ch, push=not no_push)`;
   reconstruct the existing print statements from `result.pushed`,
   `result.push_error`, `result.stop_error` to match current stdout
   exactly (byte-for-byte string comparison in tests is the acceptance
   bar).
3. `cli/sys.py` `shutdown`: add
   `@click.option("--no-push", is_flag=True, help="Skip pushing each
   host's changes to GitHub before shutdown.")`; call
   `app.csm.remove_all(push=not no_push)`.
4. `cli/test.py` `teardown`: the function already fetches
   `ch = CodeHost.query.filter_by(service_name=username).first()` a few
   lines after the `s.stop()` call — reorder so `ch` is resolved before
   the stop call, then call `app.csm.stop_host(ch, push=False)` in place
   of `s.stop()` when `ch` exists; if `ch` is `None` (shouldn't normally
   happen for a test student with a live service) fall back to the
   direct `s.stop()`.
5. `cli/node.py`: no functional change required; optionally add a
   one-line comment near `rebalance`'s own `CodeHostRepo(...).push()`
   call noting it shares the ticket-001 timeout default, for
   future-reader clarity.

### Files to create / modify

- `cspawn/cli/host.py` (`stop`, `purge`)
- `cspawn/cli/sys.py` (`shutdown`)
- `cspawn/cli/test.py` (`teardown`)
- `cspawn/cli/node.py` (comment only, optional — no functional change)
- Tests: new or extended CLI tests using Click's `CliRunner`, following
  the established pattern in `test/test_node_op_cli.py` for testing a
  `cspawnctl` subcommand in this repo, mocking
  `app.csm.stop_host` / `app.csm.remove_all`.

### Testing plan

- `CliRunner.invoke()` against each modified command, mocking
  `get_app(ctx)` / `app.csm` (`unittest.mock.patch` or a `MagicMock`
  app, following `test/test_node_op_cli.py`'s conventions).
- `host purge`: assert the new implementation's stdout matches the
  acceptance-criteria-specified strings for pushed / push-failed /
  stop-failed / dry-run cases, using a mocked `stop_host` returning
  controlled `StopResult`s.
- `--no-push` tests for `host stop` and `sys shutdown`: assert
  `stop_host` / `remove_all` is invoked with `push=False`.
- `test teardown`: assert `stop_host` is invoked with `push=False`
  always, regardless of any flag.
- Run `uv run pytest test/ -v`.

### Documentation updates

- `--help` text for the new `--no-push` flags is auto-generated by
  Click from the `help=` string on each `click.option` — no separate
  action needed.
- Confirm during implementation whether README.md's CLI section (if any
  documents individual `cspawnctl` subcommands in detail) needs a
  `--no-push` mention; as of this sprint's research, README.md does not
  document CLI flags at that level of detail.
- No change to `clasi/issues/push-on-host-stop.md` itself — the issue
  stays open until sprint close, which the team-lead handles.
