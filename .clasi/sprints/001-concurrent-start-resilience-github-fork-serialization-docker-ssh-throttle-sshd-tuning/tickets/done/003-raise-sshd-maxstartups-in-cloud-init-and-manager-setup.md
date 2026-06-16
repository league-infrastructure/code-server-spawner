---
id: '003'
title: Raise sshd MaxStartups in cloud-init and manager setup
status: done
use-cases:
- SUC-003
depends-on: []
issue: ''
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Raise sshd MaxStartups in cloud-init and manager setup

## Description

The sshd default `MaxStartups 10:30:100` means the kernel starts
probabilistically dropping new SSH handshakes once 10 are in flight.
Even with the app-level semaphore (ticket 002, cap 4), the manager node
should be configured to tolerate burst SSH connections from other sources
(monitoring, CLI operators, other tools) without dropping connections.

Raise `MaxStartups` to `30:60:100` in both the cloud-init YAML (for new
nodes) and the manager setup script (for the existing `swarm1` manager).

Also review the UFW port-22 rule in both files: current scripts use
`ufw allow 22/tcp` (a plain allow rule, not `ufw limit 22/tcp`). No
rate-limiting is in place on port 22; no UFW change is needed. Confirm
and document this finding.

## Acceptance Criteria

- [x] `config/cloud-init/swarm-node-init-v2.yaml` contains a `write_files`
      entry that writes `/etc/ssh/sshd_config.d/99-swarm.conf` with the
      line `MaxStartups 30:60:100`.
- [x] The cloud-init `runcmd` section restarts sshd after writing the
      config (e.g., `systemctl restart ssh` or `systemctl restart sshd`).
- [x] `config/host-scripts/manager-setup-swarm.sh` contains a block that
      writes the same `MaxStartups 30:60:100` line to
      `/etc/ssh/sshd_config.d/99-swarm.conf` and restarts sshd.
- [x] `manager-setup-swarm.sh` includes a comment noting that `swarm1`
      requires this script to be re-run manually (since cloud-init already
      ran on the existing node).
- [x] UFW review: both `firewall.sh` and `swarm-node-init-v2.yaml` use
      `ufw allow 22/tcp` (not `ufw limit`). This is confirmed and documented
      with a comment in the relevant script; no UFW rule change is made.

## Implementation Plan

### Approach

**cloud-init (`config/cloud-init/swarm-node-init-v2.yaml`)**

Add to the `write_files` section:

```yaml
  - path: /etc/ssh/sshd_config.d/99-swarm.conf
    permissions: '0644'
    content: |
      # Raised from default 10:30:100 to tolerate SSH bursts under load.
      MaxStartups 30:60:100
```

Add to the `runcmd` section (after the existing Docker + UFW setup):

```yaml
  # Raise sshd MaxStartups and restart
  - systemctl restart ssh || systemctl restart sshd
```

**Manager setup script (`config/host-scripts/manager-setup-swarm.sh`)**

Add a block near the end of the script (after network and swarm setup):

```bash
# --- sshd tuning (MaxStartups) ---
# NOTE: For the existing swarm1 manager, re-run this script or apply manually,
# since cloud-init already ran on that node.
echo "Configuring sshd MaxStartups 30:60:100"
mkdir -p /etc/ssh/sshd_config.d
cat > /etc/ssh/sshd_config.d/99-swarm.conf <<'EOF'
# Raised from default 10:30:100 to tolerate SSH bursts under load.
MaxStartups 30:60:100
EOF
systemctl restart ssh || systemctl restart sshd
```

**UFW review**

Inspect `config/host-scripts/firewall.sh` and `config/cloud-init/swarm-node-init-v2.yaml`:
- `firewall.sh` uses `ufw allow 22/tcp` (line 29).
- `swarm-node-init-v2.yaml` cloud-init script also uses `ufw allow 22/tcp` (line 31).
- Neither uses `ufw limit 22/tcp`.
- No change is needed. Add a brief comment in `firewall.sh` confirming this:
  ```bash
  # Plain allow (not 'limit') — no rate-limit on SSH port.
  ufw allow 22/tcp
  ```

### Files to Modify

- `config/cloud-init/swarm-node-init-v2.yaml`
  - Add `write_files` entry for `/etc/ssh/sshd_config.d/99-swarm.conf`.
  - Add `runcmd` entry to restart sshd.
- `config/host-scripts/manager-setup-swarm.sh`
  - Add sshd tuning block with comment about existing `swarm1`.
- `config/host-scripts/firewall.sh`
  - Add clarifying comment on the `ufw allow 22/tcp` line.

### Files to Create

None.

### Testing Plan

- Boot a new node using the updated cloud-init and verify:
  `sshd -T | grep -i maxstartups` returns `maxstartups 30:60:100`.
- On `swarm1`, re-run `manager-setup-swarm.sh` and verify the same.
- Verify sshd restarts without error: `systemctl status ssh`.
- Confirm existing SSH access to `swarm1` is uninterrupted after the restart.
- Run `uv run pytest` to confirm no Python code was inadvertently broken.

### Documentation Updates

The comment added to `manager-setup-swarm.sh` serves as the documentation
for the manual step required on existing nodes. No separate doc update needed.
