---
status: done
sprint: 009
tickets:
- 009-001
- 009-002
- 009-003
---

# Node provisioning silently skips cloud-init in the container (Dockerfile omits config/), yielding nodes with broken SSH

## Summary

Nodes created from **inside the spawner container** (autoscaler scale-up, any
in-container `node expand`) are provisioned **without cloud-init user-data**,
because the image does not contain the cloud-init file. The expand code treats
the missing file as a warning and proceeds
([node.py:1178-1184](cspawn/cli/node.py#L1178-L1184)), so the droplet boots as
a raw `docker-20-04` marketplace image:

- The factory **`ufw limit 22/tcp`** rule stays in place. The spawner's own
  SSH traffic (docker-over-SSH tunnels for sync/push/exec) trips the rate
  limit, sshd flaps between accepting and refusing, and every code host on the
  node sticks at "starting" because container inspection over
  `ssh://root@<node>` fails (`BrokenPipeError`, "Node manager is None").
  A reboot resets the limit counters, which masks the problem temporarily.
- The **docker-ce version pin never runs** (node joins with whatever the
  marketplace image ships, e.g. 29.6.0 vs the pinned 29.6.1).
- The sshd `MaxStartups` tuning and swarm-dataplane UFW rules from
  `config/cloud-init/swarm-node-init-v2.yaml` are never applied.

Observed live on prod 2026-07-05: hosts `bilkton` and `eric-busboom` stuck
"starting" on node swarm3; swarm3 refused SSH until rebooted; docker 29.6.0.
This is the mechanism behind the long-running "nodes intermittently
un-introspectable" problem (see also the stale-node issue resolved in sprint
008 — orphaned tasks are a downstream casualty of nodes going dark).

## Root cause

`_create_droplet` resolves the cloud-init file as
`find_parent_dir()/config/cloud-init/<DO_CLOUD_INIT>`
([node.py:1171-1186](cspawn/cli/node.py#L1171-L1186)). In the container,
[docker/Dockerfile](docker/Dockerfile) copies `cspawn/`, `migrations/`,
`data/`, and selected `docker/` files — **`config/` is never copied** (the
image was deliberately made secret-free in sprint 002, and `config/` also
holds SOPS secrets). So `DO_CLOUD_INIT=swarm-node-init-v2.yaml` resolves to a
nonexistent path, the code logs
`CLOUD_INIT_FILE not found at ...; proceeding without user-data`, and creates
the droplet bare.

## Scope / acceptance criteria

- [ ] The image contains the cloud-init templates: copy **only**
  `config/cloud-init/` (plain YAML, no secrets) into the image at the path
  `_create_droplet` resolves (`/app/config/cloud-init/`). Do NOT copy the rest
  of `config/` (SOPS secrets) — the image must stay secret-free.
- [ ] **Fail loudly, not silently**: when `DO_CLOUD_INIT` is configured but
  the file cannot be found, node creation must ABORT with a clear error
  (exception / non-zero exit) instead of proceeding without user-data. A node
  without its provisioning is worse than no node. (Keep the
  no-`DO_CLOUD_INIT`-configured case as a plain proceed — that's an explicit
  operator choice.)
- [ ] Post-join provisioning verification in `node expand` (and the
  autoscaler's scale-up path): after the node joins the swarm, verify
  (a) SSH reachability over several consecutive attempts,
  (b) `docker --version` on the node matches the pinned version,
  (c) cloud-init reported `status: done`.
  Log results; fail/alert loudly on mismatch so a defective node can never
  silently receive hosts.
- [ ] Tests: unit test that the missing-file path raises (mocked fs); test
  that the found-file path passes user_data to the droplet create call;
  image-build assertion or test that `/app/config/cloud-init/*.yaml` exists in
  the built image (e.g. a Dockerfile RUN check or CI step).

## Notes

- Ops workaround until fixed: run `cspawnctl node expand` from a full local
  checkout (file resolves fine there).
- Related trap discovered during diagnosis: the config loader gives
  `os.environ` precedence over `.env`
  ([config.py:174-177](cspawn/util/config.py#L174-L177)), so a stale
  `DO_TOKEN` exported in an operator's shell profile silently overrides the
  correct dotconfig value and fails DO API calls. Consider warning when an
  env var shadows a differing `.env` value for high-stakes keys. (Optional,
  may split to its own issue.)
