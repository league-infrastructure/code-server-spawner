---
id: '013'
title: "Warm new nodes \u2014 pre-pull codehost images at expand + snapshot staleness\
  \ check + cloud-init idempotency"
status: done
branch: sprint/013-warm-new-nodes-pre-pull-codehost-images-at-expand-snapshot-staleness-check-cloud-init-idempotency
use-cases: []
issues:
- warm-new-nodes-prepull-and-snapshot-integration.md
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 013: Warm new nodes — pre-pull codehost images at expand + snapshot staleness check + cloud-init idempotency

## Goals

Integrate the golden DO snapshot (docker-ce baked and held, id `235956540`,
docker 29.6.1) with the spawner's node-expand flow so that:

1. A freshly-joined node never accepts scheduled hosts before its code-server
   image is warm — killing the cold-pull 503 herd confirmed live 2026-07-06.
2. An operator running a golden-snapshot fleet gets a loud, actionable
   diagnostic the moment a node's baked docker-ce drifts from the manager's,
   naming the exact remedy (rebuild the snapshot).
3. Cloud-init's docker-ce pin step becomes a true no-op on a snapshot node
   (docker already installed + held at the right major) instead of repeating
   a full apt round-trip at every boot, while a non-snapshot or wrong-major
   node still gets the complete sprint-012 hardened install untouched.

## Problem

Today a freshly-joined swarm node has an empty local image cache. When the
scheduler lands code-server hosts on it, they all block in `Preparing` while
the node pulls the ~1.5GB compressed code-server image, and Caddy 503s those
students until the pull finishes (confirmed live 2026-07-06). Separately, the
golden snapshot's baked docker-ce version is frozen at snapshot-build time and
can silently drift from the manager's as the manager is upgraded over time;
today's post-join verify catches a real major mismatch but doesn't tell the
operator *why* it happened or what to do about it. Finally, the sprint-012
hardened docker-ce pin install in cloud-init unconditionally re-runs
`apt-get update` + a full install/hold round-trip on every single boot, even
on a snapshot node where docker-ce is already installed and held at the
correct version — a needless, non-zero-cost step at every node expand.

## Solution

Three surgical, independent code changes (no deploy in this sprint):

- **Part A (ticket 001):** In `expand()`'s post-join flow (`cspawn/cli/node.py`)
  and in `apply_plan()`'s scale-up loop (`cspawn/cs_docker/autoscale.py`,
  which does **not** share code with `expand()` — it duplicates the
  create/configure/join/verify sequence inline, calling the same
  `cspawn.cli.node` helper functions — see architecture-update.md for the
  full finding): immediately after swarm membership is confirmed, drain the
  new node; after the existing post-join `_verify_node_provisioning` hard
  gate passes, best-effort pre-pull `SELECT DISTINCT image_uri FROM
  class_proto` (unioned with an optional `NODE_PREPULL_IMAGES` config
  allowlist) via `ssh root@<fqdn> docker pull <image>`, bounded by a timeout;
  then re-activate the node. A pull failure logs a WARNING and the node is
  activated anyway (best-effort, not a new hard gate).
- **Part C1 (ticket 002):** At the same post-join point, when the node's
  docker-ce major differs from the manager's live major, log a WARNING
  naming the likely cause ("provisioning from a golden snapshot whose baked
  docker has drifted") and the remedy (`scripts/build-golden-node-snapshot.sh`).
  This is purely diagnostic — `_verify_node_provisioning` remains the
  unchanged hard gate on a real major mismatch.
- **Part C2 (ticket 003):** In `config/cloud-init/swarm-node-init-v2.yaml`,
  guard the sprint-012 hardened docker-ce pin block with a precheck: if
  `docker --version`'s major already equals the pin's major, skip the
  stop/mask + install + hold + unmask round-trip entirely. A missing docker
  (non-snapshot node) or a present-but-wrong-major docker still runs the full
  hardened install and fail-loud path exactly as sprint 012 shipped it.

## Success Criteria

- A new node never appears schedulable (`Availability=active`) with a cold
  image cache — it is provably drained before the pre-pull attempt and only
  reactivated afterward, whether the pull succeeds or fails.
- `apply_plan`'s autoscale scale-up path gets the identical warm-up behavior
  as the manual `cspawnctl node expand` CLI path — not a divergent or missing
  copy.
- A docker major mismatch on a freshly-joined node produces both (a) the
  existing hard verification failure/drain (unchanged) and (b) a new WARNING
  naming golden-snapshot staleness as the likely cause and the rebuild script
  as the remedy.
- `swarm-node-init-v2.yaml`'s docker-ce pin block is a true no-op (no
  `apt-get update`, no install, no hold/unmask cycle) when docker already
  matches the pin's major; a missing or wrong-major docker still runs the
  complete sprint-012 hardened path unchanged.
- Full suite green (`uv run pytest --ignore=test/test_admin_coverage.py -q`;
  that file has known pre-existing PRODUCTION-env failures, unrelated to this
  sprint).

## Scope

### In Scope

- `cspawn/cli/node.py`: new module-level helpers for image-list resolution,
  bounded-timeout SSH `docker pull`, symmetric node-activate (there is
  currently no "set active" counterpart to `_drain_swarm_node`), and the
  docker-staleness WARNING check; wiring these into `expand()`'s existing
  post-join sequence.
- `cspawn/cs_docker/autoscale.py`: the same wiring into `apply_plan()`'s
  scale-up loop, importing the new helpers the same way it already imports
  `_verify_node_provisioning`/`_drain_swarm_node`/etc. from `cspawn.cli.node`.
- `cspawn/models.py`: read-only use of the existing `ClassProto.image_uri`
  column — no schema change.
- `config/cloud-init/swarm-node-init-v2.yaml`: idempotency guard around the
  existing sprint-012 hardened docker-ce pin block.
- Unit tests: `test/test_node_provisioning_verify.py`,
  `test/test_node_op_cli.py`, `test/test_autoscale.py`,
  `test/test_node_cloud_init.py` — all with mocked ssh/DB/docker clients,
  matching each file's existing conventions.

### Out of Scope

- Baking the code-server image into the golden snapshot (deliberately
  pre-pulled instead — see `docs/golden-node-snapshot.md`).
- Dynamic "pin to manager's exact live version" templating (already shipped,
  sprint 012).
- Rebalancing, host-pinning, or capacity/scale-plan policy changes.
- **Deploy-time work**: setting `DO_IMAGE=235956540` in the prod config
  (dotconfig) and the single-node validation test described in
  `docs/golden-node-snapshot.md`. This is an operator action after this
  sprint's code merges and a new spawner image is built — not part of this
  sprint's code/test deliverable.
- A new CLI surface for manually activating a drained node (the
  `_activate_swarm_node` helper added here is internal to the expand/
  autoscale flow only; the top-level `node drain`/`add`/`rm` CLI stubs in
  `cspawn/cli/node.py` are pre-existing dead stubs, unrelated to and
  unmodified by this sprint).

## Test Strategy

Entirely unit-level, mirroring existing conventions in each touched test
file: DO/Docker/SSH/DB clients are mocked (paramiko SSH calls, `docker.
DockerClient`, `digitalocean.Manager`, Flask app/DB session); no live
droplet, swarm, or SSH connection is exercised. Cloud-init tests remain
content-assertion tests against the real shipped YAML (no live `apt`/`dpkg`
execution), consistent with `test/test_node_cloud_init.py`'s existing style.
Key new assertions:
- Ordering assertions (drain called before pull; activate called only after
  pull attempts, regardless of individual pull success/failure).
- The `apply_plan` scale-up path gets equivalent test coverage to `expand()`
  for the same new behavior (matching the existing pattern where
  `TestApplyPlanScaleUpVerification` mirrors `TestExpandVerification*`).
- Best-effort semantics: a failed image pull does not prevent activation or
  fail the command/batch item.
- Cloud-init: both branches (skip-when-matching, full-install-when-missing-
  or-wrong-major) asserted via content/structure checks on the rendered YAML,
  consistent with the existing `_v2_runcmd_text()` helper.

Run with `uv run pytest --ignore=test/test_admin_coverage.py -q` (that file
has known pre-existing PRODUCTION-env failures, unrelated to this sprint —
ignore).

## Architecture Notes

See `architecture-update.md` for the full 7-step analysis. Headline finding:
`apply_plan()` (autoscale.py) does **not** call the `expand()` CLI command —
it duplicates the create/configure/join/verify sequence inline against the
same `cspawn.cli.node` helper functions. This sprint's new behavior (drain→
pre-pull→activate, staleness WARNING) must therefore be wired into **both**
call sites explicitly; it does not come "for free" from a shared code path.
This mirrors the codebase's existing precedent (the sprint-009/012
verify-and-drain-on-failure logic is likewise duplicated across both sites,
not extracted into one shared orchestration function) — this sprint follows
that same precedent rather than introducing a larger, riskier refactor.

## GitHub Issues

None linked yet. This sprint's work originates from
`clasi/issues/warm-new-nodes-prepull-and-snapshot-integration.md` (see
`issues:` in this file's frontmatter) — no separate GitHub issue tracks it.

## Definition of Ready

Before tickets can be created, all of the following must be true:

- [x] Sprint planning documents are complete (sprint.md, use cases, architecture)
- [x] Architecture review passed (self-review verdict: APPROVE WITH CHANGES — see
  `architecture_review` gate notes)
- [x] Stakeholder has approved the sprint plan (`stakeholder_approval` gate
  recorded by team-lead; sprint advanced to `ticketing`)

Four team-lead decisions were baked into the tickets below during ticket
creation:
1. `NODE_PREPULL_IMAGES` is a strict **union** with the DB-derived distinct
   `class_proto.image_uri` list — config only adds images, never replaces
   or narrows DB-derived coverage (ticket 001).
2. `NODE_PREPULL_TIMEOUT_S` default **300s** (tunable) (ticket 001).
3. `apply_plan`'s missing swarm-membership-wait (an asymmetry vs.
   `expand()`'s explicit 300s poll loop) is left as-is — noted in ticket 001
   and here; out of scope for this sprint.
4. **Reactivation failure must be loud**: `_activate_swarm_node` retries
   with backoff, and logs at **ERROR** (not WARNING) on final failure,
   naming the manual remedy `docker node update --availability active
   <node>` — a warmed-but-stuck-drained node is silently-wasted capacity,
   the exact failure class this sprint prevents (ticket 001).

## Tickets

| # | Title | Depends On |
|---|-------|------------|
| 001 | Pre-pull codehost images at node-expand (drain, warm, activate) | — |
| 002 | Snapshot staleness WARNING on docker major mismatch | — |
| 003 | Cloud-init docker-ce pin idempotency guard | — |

Tickets execute serially in the order listed above, but note they are
largely independent (no `depends-on` edges) — see architecture-update.md
Step 2 for why M1/M2/M3 don't depend on each other. Full ticket content
(description, acceptance criteria, implementation plan, testing) lives in
`tickets/001-pre-pull-codehost-images-at-node-expand-drain-warm-activate.md`,
`tickets/002-snapshot-staleness-warning-on-docker-major-mismatch.md`, and
`tickets/003-cloud-init-docker-ce-pin-idempotency-guard.md`.
