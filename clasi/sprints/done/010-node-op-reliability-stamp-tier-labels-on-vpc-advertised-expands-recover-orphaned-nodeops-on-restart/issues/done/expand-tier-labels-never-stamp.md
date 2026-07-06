---
status: done
sprint: '010'
tickets:
- 010-001
---

# `node expand` never stamps cs.tier/cs.capacity labels (matches node by public IP, but Status.Addr is the VPC address)

## Summary

The tier-labeling block at the end of `_join_swarm`
([node.py:1651-1678](cspawn/cli/node.py#L1651-L1678)) finds the just-joined
node by comparing each swarm node's `Status.Addr` to the droplet's **public
IP**. But nodes join with `--advertise-addr <10.124.x.x>` (VPC address), so
`Status.Addr` is always the **private** address and the comparison never
matches. The loop spins for its 90s deadline and gives up inside
`except: pass` — no log, no error. Result: `cs.tier`/`cs.capacity` labels are
never applied by expand, and the admin Nodes tab shows `---` for Tier and
Capacity on every node created or re-created through expand. Anything else
that consumes those labels (autoscaler capacity math via `cs.capacity`) sees
the node as tierless.

Observed live 2026-07-06: both expand runs of the day (small and large tiers)
stamped only `code-host-user=true`; `cs.tier`/`cs.capacity` were missing on
swarm3 (and swarm2 had no labels at all). Restored manually via
`node label-backfill --apply`.

Re-confirmed 2026-07-06 ~15:15 UTC: a freshly re-expanded swarm4 (large tier,
`s-8vcpu-16gb-amd`, `Status.Addr=10.124.0.6` vs public `164.92.116.173`, already
carrying 9 hosts) again stamped only `code-host-user=true` — Tier/Capacity blank
in the Nodes tab. swarm3 was still unlabeled from earlier. Both relabeled by hand
this session: swarm3 → `cs.tier=small`/`cs.capacity=6`, swarm4 →
`cs.tier=large`/`cs.capacity=14`. This is a deterministic regression on every
VPC-advertised expand, not a one-off. Note the missing `cs.capacity` is not purely
cosmetic: `node_capacity()` ([tiers.py:101-109](cspawn/cs_docker/tiers.py#L101-L109))
falls back to `DEFAULT_CAPACITY` (6) when the label is absent, so a large node
(true capacity 14) is under-counted by the autoscaler until backfilled.

## Fix

- In the cs.tier block, reuse the same matching strategy as the
  `code-host-user` block directly above it
  ([node.py:1620-1647](cspawn/cli/node.py#L1620-L1647)): match by
  hostname/short-name (with the name-guess fallback), not by public IP.
  Alternatively match `Status.Addr` against the droplet's **private** VPC
  address; hostname matching is simpler and already proven by the adjacent
  block.
- Log a WARNING when the deadline expires without applying labels, instead of
  silent `except: pass` — this failure was invisible for an unknown number of
  node generations.
- Consider folding label verification into the sprint-009
  `_verify_node_provisioning` post-join check (`cs.tier` present) so a
  labeling regression fails loudly at creation time.

## Acceptance criteria (draft)

- [ ] A full `node expand --tier <t>` on a VPC-advertised swarm stamps
  `cs.tier`/`cs.capacity` (unit test with mocked manager client whose
  `Status.Addr` is a private address different from the droplet's public IP).
- [ ] Label-application timeout logs a WARNING naming the node.
- [ ] Admin Nodes tab shows Tier/Capacity for newly expanded nodes without
  manual `label-backfill`.
