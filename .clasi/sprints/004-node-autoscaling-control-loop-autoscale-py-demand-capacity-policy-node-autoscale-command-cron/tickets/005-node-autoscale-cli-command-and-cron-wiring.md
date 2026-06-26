---
id: '005'
title: node autoscale CLI command and cron wiring
status: done
use-cases:
- SUC-001
- SUC-002
- SUC-003
depends-on:
- '004'
github-issue: ''
issue: node-autoscaling-control-loop.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# node autoscale CLI command and cron wiring

## Description

Wire `run_autoscale` to the `cspawnctl node autoscale` Click command and add the
commented-out cron line to `docker/crontab`. This is the final integration ticket:
the autoscaler becomes invocable from the command line and from cron. The safety
defaults (`AUTOSCALE_ENABLED=false`, `AUTOSCALE_DRY_RUN=true`, cron line commented
out) ensure that merging and deploying this sprint does not provision or destroy
anything until an operator explicitly enables it.

## Acceptance Criteria

- [x] `@node.command(name="autoscale")` subcommand is added to the `node` group in
      `cspawn/cli/node.py`.
- [x] Command options:
      - `-N` / `--dry-run` (flag): override `dry_run=True` regardless of config.
      - `--force` (flag): bypass cooldown in `plan_scale_down` (for manual emergency use).
      - `--up-only` / `--down-only` (mutex flag pair, default neither): limit the cycle
        to scale-up actions only or scale-down actions only.
- [x] Command body calls `run_autoscale(ctx, dry_run=dry_run, force=force, up_only=up_only)`
      and calls `click.echo(result.summary())`.
- [x] `cspawnctl node autoscale --help` exits 0 and shows all options.
- [x] `cspawnctl -d devel node autoscale --dry-run` (or equivalent local invocation)
      runs without traceback when `AUTOSCALE_ENABLED=false` (logs "autoscale disabled"
      and exits 0). CRITICAL: do not require live Docker or DO for this check.
- [x] `docker/crontab` contains a commented-out autoscale line:
      ```
      # */2 * * * * cspawnctl -d prod node autoscale >/proc/1/fd/1 2>/proc/1/fd/2
      ```
      positioned alongside the existing commented `host reap` line. The comment explains
      that uncommenting activates the autoscaler (which also requires setting
      `AUTOSCALE_ENABLED=true` and `AUTOSCALE_DRY_RUN=false` in config).
- [x] `uv run pytest` passes with no regressions.
- [x] `AUTOSCALE_ENABLED=false` in all three `public.env` files (verified from ticket 001).
- [x] `AUTOSCALE_DRY_RUN=true` in all three `public.env` files (verified from ticket 001).

## CRITICAL Safety Requirement

This sprint makes the cluster capable of scaling itself. Before this ticket is marked
done, the implementer must verify all three independent safety layers:

1. `AUTOSCALE_ENABLED=false` in `config/prod/public.env` — kill-switch at application layer.
2. `AUTOSCALE_DRY_RUN=true` in `config/prod/public.env` — dry-run override at application layer.
3. The cron line in `docker/crontab` is **commented out** — no cron trigger at infrastructure layer.

All three must be true before this ticket can be closed.

## Implementation Plan

### Approach

Add the `autoscale_cmd` Click command to `node.py` near the `contract_node` command
(logical grouping: both are scaling operations). Import `run_autoscale` inside the
command body (lazy import avoids circular import risk and keeps the CLI module lightweight).

### Files to Modify

- `cspawn/cli/node.py` — add `autoscale_cmd` subcommand.
- `docker/crontab` — add commented-out autoscale cron line with explanatory comment.

### Command Implementation Sketch

```python
@node.command(name="autoscale")
@click.option("-N", "--dry-run", is_flag=True,
              help="Read-only mode: log the plan but make no changes.")
@click.option("--force", is_flag=True,
              help="Bypass cooldown check (for manual emergency use).")
@click.option("--up-only/--down-only", "up_only", default=None,
              help="Limit to scale-up-only or scale-down-only actions.")
@click.pass_context
def autoscale_cmd(ctx, dry_run: bool, force: bool, up_only):
    """Run one autoscale cycle: assess cluster demand and scale up or down.

    Respects AUTOSCALE_ENABLED (kill-switch) and AUTOSCALE_DRY_RUN (global
    dry-run override). Safe to run from cron: exits cleanly when disabled.
    """
    from cspawn.cs_docker.autoscale import run_autoscale
    result = run_autoscale(ctx, dry_run=dry_run, force=force, up_only=up_only)
    click.echo(result.summary())
```

### Crontab Addition

```
# Autoscale worker nodes every 2 minutes.
# To activate: uncomment AND set AUTOSCALE_ENABLED=true + AUTOSCALE_DRY_RUN=false in config.
# Start with AUTOSCALE_DRY_RUN=true (read-only) for at least one week before enabling live scaling.
# */2 * * * * cspawnctl -d prod node autoscale >/proc/1/fd/1 2>/proc/1/fd/2
```

### Testing Plan

- `uv run pytest` — full suite, no regressions.
- Manually verify: `cspawnctl node --help` lists `autoscale`; `cspawnctl node autoscale --help`
  shows all options.
- Manually verify: with `AUTOSCALE_ENABLED=false` in devel config, running
  `cspawnctl -d devel node autoscale --dry-run` exits 0 with a "disabled" log message and
  no exceptions.

### Documentation Updates

Add a comment block in `docker/crontab` above the autoscale line explaining the phased
rollout steps (Phase 0: dry-run watch → Phase 1: enable → Phase 2: disable dry-run).
