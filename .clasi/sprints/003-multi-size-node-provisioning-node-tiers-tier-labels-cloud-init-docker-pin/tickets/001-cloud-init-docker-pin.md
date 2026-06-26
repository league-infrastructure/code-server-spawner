---
id: '001'
title: cloud-init docker pin
status: open
use-cases: [SUC-003]
depends-on: []
github-issue: ''
issue: multi-size-node-provisioning.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# cloud-init docker pin

## Description

Edit `config/cloud-init/swarm-node-init-v2.yaml` to stop installing `docker.io` and
instead pin docker-ce to the manager's installed version (27.4.1 today). This eliminates
the Docker major-version mismatch that currently aborts automated swarm joins and requires
a manual `apt-get install --allow-downgrades ... && apt-mark hold && systemctl restart docker`
runbook on every new node.

The DO `docker-20-04` image ships a newer docker engine than the swarm manager runs (27.4.1).
The cloud-init `packages: [docker.io]` line also conflicts with the image's docker-ce.
The version-mismatch preflight check in `_join_swarm` (node.py:974) aborts joins when
the manager and worker major versions differ. Pinning docker-ce to 27.4.1 in cloud-init
makes fresh nodes boot with the correct version so no manual intervention is needed.

This ticket is independent of all other sprint changes and addresses the most pressing
operational blocker.

## Acceptance Criteria

- [ ] `packages:` in `swarm-node-init-v2.yaml` no longer lists `docker.io`.
- [ ] `ufw` and `jq` remain in `packages:`.
- [ ] `runcmd:` begins with `apt-get update -qq`.
- [ ] `runcmd:` installs `docker-ce=5:27.4.1-1~ubuntu.20.04~focal` and `docker-ce-cli=5:27.4.1-1~ubuntu.20.04~focal` with `--allow-downgrades --allow-change-held-packages` before `systemctl enable --now docker`.
- [ ] `runcmd:` runs `apt-mark hold docker-ce docker-ce-cli` before `systemctl enable --now docker`.
- [ ] `systemctl enable --now docker` is still present (after the pin/hold steps).
- [ ] All existing `runcmd` steps (do-agent curl, `configure-ufw-swarm.sh`, sshd restart) are preserved in their current order after the docker steps.
- [ ] A comment in `runcmd` says "Update this version when the manager's docker-ce is upgraded."
- [ ] The `write_files` section is unchanged.

## Implementation Plan

### Approach

Single-file surgical edit to `config/cloud-init/swarm-node-init-v2.yaml`. No Python changes.

**Step 1**: Remove `docker.io` from `packages:`. Keep `ufw` and `jq`.

Current `packages:` block (lines 4-7):
```yaml
packages:
  - docker.io
  - ufw
  - jq
```

Becomes:
```yaml
packages:
  - ufw
  - jq
```

**Step 2**: Prepend docker pin steps to `runcmd:`. The current block starts at line 88:
```yaml
runcmd:
  # Make sure Docker starts
  - systemctl enable --now docker
  ...
```

Replace it with:
```yaml
runcmd:
  # Pin docker-ce to the manager's version to prevent major-version mismatch at join.
  # The DO docker-20-04 image ships a newer engine; this downgrades and holds it.
  # IMPORTANT: Update this version string when the manager's docker-ce is upgraded.
  - apt-get update -qq
  - >-
    apt-get install -y --allow-downgrades --allow-change-held-packages
    docker-ce=5:27.4.1-1~ubuntu.20.04~focal
    docker-ce-cli=5:27.4.1-1~ubuntu.20.04~focal
  - apt-mark hold docker-ce docker-ce-cli
  - systemctl enable --now docker

  # Install the DigitalOcean metrics agent (do-agent). The install script
  # adds DO's package repo and enables the do-agent service automatically.
  - curl -sSL https://repos.insights.digitalocean.com/install.sh | bash

  # Configure firewall for swarm (workers do not need 2377)
  - /usr/local/sbin/configure-ufw-swarm.sh

  # Raise sshd MaxStartups and restart
  - systemctl restart ssh || systemctl restart sshd
```

### Files to Modify

- `/Users/eric/proj/league/code-server-mono/code-server-spawner/config/cloud-init/swarm-node-init-v2.yaml`
  - Lines 4-7: remove `- docker.io` from `packages:`.
  - Lines 88-101: prepend docker pin steps to `runcmd:`.

### Files to Create

None.

### Testing Plan

**Automated**: No unit tests apply to a cloud-init YAML. Run `uv run pytest` to confirm
no existing tests break (none touch this file).

**Manual verification** (recommended before first prod provisioning):
1. Provision a test droplet with the updated cloud-init.
2. SSH in after boot: run `docker version` — confirm `Server.Version: 27.4.1`.
3. Run `apt-mark showhold` — confirm `docker-ce` and `docker-ce-cli` appear.
4. Run `ufw status` and `docker info` to confirm ufw and docker are both active.

**Risk**: The apt epoch/suffix `5:27.4.1-1~ubuntu.20.04~focal` is Ubuntu-release-specific.
Verify with `apt-cache madison docker-ce` on a fresh `docker-20-04` droplet if the
install fails. See Open Question #4 in `architecture-update.md`.

### Documentation Updates

The inline comment added to `runcmd:` is the only documentation needed. No README
or other doc changes required for this ticket.
