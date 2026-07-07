---
status: done
sprint: '013'
tickets:
- 013-001
- 013-002
- 013-003
---

# Warm new nodes at expand: pre-pull codehost images + snapshot staleness check + cloud-init idempotency

## Summary

Integrates the golden node snapshot (docker baked, id `235956540`, docker
29.6.1 held; built by `scripts/build-golden-node-snapshot.sh`) with the spawner.
The snapshot deliberately does **not** bake the code-server image; instead new
nodes pre-pull the *current* image at expand — so a `make release` of
`docker-codeserver-python` never requires a snapshot rebuild (see
`docs/golden-node-snapshot.md`). Three changes, plus a deploy-time config step.

## Part A — pre-pull codehost images at node-expand (kills the cold-pull 503 herd)

Today a freshly-joined node has an empty image cache; when the scheduler lands
hosts on it they all block in `Preparing` while it pulls the ~1.5GB (compressed)
code-server image, and Caddy 503s those students until it finishes (live
2026-07-06). Fix: warm the node **before** it accepts hosts.

- In the expand flow (`cspawn/cli/node.py`), **after** the node joins: set the
  new node `drain` (unschedulable), pre-pull the image(s), then set it `active`.
  Draining first guarantees no host lands mid-pull. (The expand flow already has
  manager access to set node availability.)
- **Which images:** `SELECT DISTINCT image_uri FROM class_proto` (the images any
  host might use — usually just the one code-server image), plus an optional
  `NODE_PREPULL_IMAGES` config allowlist/override for explicit control.
- **How to pull:** the spawner container has `ssh` + key but **no docker CLI**
  (see [[spawner-has-no-docker-cli-use-ssh-node-local]]) — so pull via
  `ssh root@<node-fqdn> docker pull <image>`, the same node-local-docker pattern
  the push() fix uses. The image is **public on ghcr** (anon manifest → 200), so
  no registry auth is needed.
- **Best-effort:** a pull failure logs a WARNING and still activates the node
  (the image will pull on-demand later, as today) — pre-pull is an optimization,
  not a new hard gate. Wrap with a sane timeout so a wedged pull can't hang expand.
- Node FQDN via `NODE_HOSTNAME_TEMPLATE.format(...)`, as elsewhere.

## Part C1 — snapshot staleness check (fail loud, don't silently drift)

When `DO_IMAGE` is a golden snapshot, its baked docker version is frozen at
snapshot-build time and can drift from the manager as the manager upgrades. The
post-join major-verify already *blocks* a real major mismatch, but the operator
should be told *why* and *what to do*:

- At expand (post-join), when the node's docker major differs from the manager's,
  emit a clear WARNING naming the likely cause and remedy — e.g. "node docker
  X vs manager Y; if provisioning from a golden snapshot, rebuild it via
  scripts/build-golden-node-snapshot.sh". This complements (does not replace)
  `_verify_node_provisioning`.
- Keep it a diagnostic/warning; the existing verify remains the hard gate on a
  major mismatch.

## Part C2 — cloud-init docker block is a no-op when docker already matches

On a snapshot node docker-ce is already installed + held at the right version.
The hardened pin block in `config/cloud-init/swarm-node-init-v2.yaml` still runs
`apt-get update` + `apt-get install docker-ce=<pin>` (a near-no-op, but a
needless round-trip at every boot). Add a guard: if `docker --version`'s
major already equals the pin's major, skip the install/mask/hold/unmask steps
(docker is already correct and held). Non-snapshot nodes (no docker yet) fall
through to the full hardened install exactly as today. Preserve the fail-loud
marker path for the case where docker is present but the *wrong* major.

## Deploy-time (NOT part of this sprint's code)

Set `DO_IMAGE=235956540` in the prod config (dotconfig), deploy the spawner,
then **test ONE node off the snapshot** (joins, image pre-pulls, a host runs)
before the fleet depends on it. Delete the old base-image assumption only after
that passes. Documented in `docs/golden-node-snapshot.md`.

## Out of scope
- Baking the code-server image into the snapshot (deliberately pre-pulled instead).
- Dynamic manager-version docker pin (already shipped).
- Rebalancing / host pinning / capacity policy.

## Acceptance criteria (draft)
- [ ] Expand pre-pulls the distinct `class_proto.image_uri`s (plus
  `NODE_PREPULL_IMAGES` if set) onto the new node via `ssh <node> docker pull`,
  while the node is drained, then sets it active. A pull failure logs a WARNING
  and still activates the node (best-effort). Unit tests with mocked ssh/DB.
- [ ] A drained→active window means no host can be scheduled before the pull
  completes (assert the ordering: drain set before pull, active only after).
- [ ] A docker major mismatch vs the manager emits a clear "snapshot may be
  stale — rebuild" WARNING in addition to the existing verify failure.
- [ ] The cloud-init docker block is skipped when docker already matches the
  pin major; a missing/wrong-major docker still runs the full hardened install.
  Tests assert both branches.
- [ ] Suite green (excluding the known pre-existing `test_admin_coverage.py`
  PRODUCTION-env failures).
