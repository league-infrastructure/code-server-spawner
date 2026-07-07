#!/usr/bin/env bash
#
# build-golden-node-snapshot.sh — Build a DigitalOcean snapshot for swarm worker nodes.
#
# WHAT THIS BAKES (and deliberately does NOT):
#   - docker-ce + docker-ce-cli, pinned to the swarm MANAGER's version and
#     `apt-mark hold`ed so automatic upgrades never drift it.
#   - ufw, jq, the do-agent metrics agent, and the swarm-node helper files
#     (configure-ufw-swarm.sh, sshd MaxStartups tuning).
#   - It does NOT bake the code-server / codehost image. That image is
#     pre-pulled at node-expand time instead (see cspawn/cli/node.py), so a
#     `make release` of docker-codeserver-python needs NO snapshot rebuild.
#     This snapshot only needs rebuilding on a docker MAJOR upgrade — which is
#     rare, deliberate, and loudly caught by _verify_node_provisioning.
#
# WHAT STILL RUNS AT BOOT (kept in the slimmed cloud-init, per node):
#   - UFW swarm-dataplane rules (they detect the per-node VPC iface at boot),
#     sshd restart, the docker-version pin (a no-op when docker is already the
#     right version from the snapshot), and the swarm join.
#
# USAGE:
#   scripts/build-golden-node-snapshot.sh [DOCKER_VERSION]
#   - DOCKER_VERSION defaults to the live swarm manager's Server.Version
#     (queried via `docker --context "$DOCKER_CONTEXT"`); pass an explicit
#     X.Y.Z to override.
#   - Requires DO_TOKEN in ./.env (or the environment). doctl must be installed.
#
# RUNBOOK: docs/golden-node-snapshot.md
set -euo pipefail

# --- config -----------------------------------------------------------------
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

DO_TOKEN="${DO_TOKEN:-$(grep -E '^DO_TOKEN' .env 2>/dev/null | sed -E "s/^DO_TOKEN=//; s/^'//; s/'\$//")}"
[ -n "${DO_TOKEN:-}" ] || { echo "FATAL: DO_TOKEN not set (env or .env)"; exit 1; }

DOCKER_CONTEXT="${DOCKER_CONTEXT:-swarm1}"
BASE_IMAGE="${BASE_IMAGE:-ubuntu-22-04-x64}"
REGION="${REGION:-sfo3}"
BUILDER_SIZE="${BUILDER_SIZE:-s-2vcpu-4gb-amd}"
SSH_KEY_ID="${SSH_KEY_ID:-57585392}"          # cspawn-swarm3-1783258162 (matches ~/.ssh/id_rsa)
SSH_KEY_FILE="${SSH_KEY_FILE:-$HOME/.ssh/id_rsa}"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
BUILDER_NAME="golden-node-builder-${STAMP}"

doctl() { command doctl "$@" --access-token "$DO_TOKEN"; }
ssh_node() { ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
                 -o ConnectTimeout=15 -o BatchMode=yes -i "$SSH_KEY_FILE" "root@$1" "${@:2}"; }
log() { echo "[$(date -u +%H:%M:%S)] $*"; }

# --- resolve the docker version to bake -------------------------------------
DOCKER_VERSION="${1:-}"
if [ -z "$DOCKER_VERSION" ]; then
  DOCKER_VERSION="$(command docker --context "$DOCKER_CONTEXT" version --format '{{.Server.Version}}' 2>/dev/null || true)"
fi
[[ "$DOCKER_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
  echo "FATAL: could not resolve a valid docker version to bake (got '${DOCKER_VERSION}'). Pass X.Y.Z explicitly."; exit 1; }
SNAPSHOT_NAME="swarm-node-golden-docker${DOCKER_VERSION}-${STAMP}"
log "Baking docker-ce ${DOCKER_VERSION}; base=${BASE_IMAGE} region=${REGION} size=${BUILDER_SIZE}"

# --- 1. create the builder droplet ------------------------------------------
log "Creating builder droplet ${BUILDER_NAME}..."
BUILDER_ID="$(doctl compute droplet create "$BUILDER_NAME" \
  --image "$BASE_IMAGE" --region "$REGION" --size "$BUILDER_SIZE" \
  --ssh-keys "$SSH_KEY_ID" --wait --no-header --format ID)"
[ -n "$BUILDER_ID" ] || { echo "FATAL: droplet create failed"; exit 1; }
log "Builder droplet id=${BUILDER_ID}"

cleanup() {
  if [ -n "${BUILDER_ID:-}" ]; then
    log "Destroying builder droplet ${BUILDER_ID}..."
    doctl compute droplet delete "$BUILDER_ID" --force || echo "WARN: failed to delete builder ${BUILDER_ID} — destroy it manually."
  fi
}
trap cleanup EXIT

BUILDER_IP="$(doctl compute droplet get "$BUILDER_ID" --no-header --format PublicIPv4)"
log "Builder IP ${BUILDER_IP}; waiting for SSH..."
for i in $(seq 1 40); do ssh_node "$BUILDER_IP" true 2>/dev/null && break; sleep 5;
  [ "$i" = 40 ] && { echo "FATAL: SSH never came up"; exit 1; }; done
log "SSH up."

# --- 2. provision (docker + prereqs), mirroring the hardened cloud-init ------
log "Provisioning builder (docker ${DOCKER_VERSION}, held; ufw/jq/do-agent; helper files)..."
ssh_node "$BUILDER_IP" "DOCKER_VERSION='${DOCKER_VERSION}' bash -s" <<'PROVISION'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

# Keep unattended-upgrades from racing the dpkg lock during the build.
systemctl stop unattended-upgrades.service apt-daily.service apt-daily.timer apt-daily-upgrade.service apt-daily-upgrade.timer 2>/dev/null || true
systemctl mask unattended-upgrades.service apt-daily.service apt-daily.timer apt-daily-upgrade.service apt-daily-upgrade.timer 2>/dev/null || true

apt-get update -qq
apt-get install -y -o DPkg::Lock::Timeout=600 ufw jq ca-certificates curl gnupg

# Docker's official apt repo.
. /etc/os-release
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" > /etc/apt/sources.list.d/docker.list
apt-get update -qq

DOCKER_PIN="5:${DOCKER_VERSION}-1~ubuntu.${VERSION_ID}~${VERSION_CODENAME}"
EXPECTED_MAJOR="${DOCKER_VERSION%%.*}"
for attempt in 1 2 3; do
  apt-get install -y --allow-downgrades --allow-change-held-packages -o DPkg::Lock::Timeout=600 \
    "docker-ce=${DOCKER_PIN}" "docker-ce-cli=${DOCKER_PIN}" containerd.io docker-buildx-plugin docker-compose-plugin && break
  echo "docker-ce install attempt ${attempt} failed; retrying" >&2; sleep $((attempt * 10))
done
apt-mark hold docker-ce docker-ce-cli
systemctl enable docker

# Fail loud if the pin didn't take.
ACTUAL="$(docker --version 2>/dev/null || true)"
ACTUAL_MAJOR="$(echo "$ACTUAL" | grep -oE '[0-9]+' | head -n1)"
if [ "$ACTUAL_MAJOR" != "$EXPECTED_MAJOR" ]; then
  echo "GOLDEN_BUILD_DOCKER_PIN_FAILED: expected major ${EXPECTED_MAJOR} (pin ${DOCKER_PIN}), got '${ACTUAL}'" >&2
  exit 1
fi
echo "docker installed + held: ${ACTUAL}"

# DigitalOcean metrics agent.
curl -sSL https://repos.insights.digitalocean.com/install.sh | bash || echo "WARN: do-agent install failed (non-fatal)"

# Bake the swarm-node helper files (rules themselves run at boot, per-node).
mkdir -p /etc/ssh/sshd_config.d /usr/local/sbin
printf '# Raised from default 10:30:100 to tolerate SSH bursts under load.\nMaxStartups 30:60:100\n' > /etc/ssh/sshd_config.d/99-swarm.conf
chmod 0644 /etc/ssh/sshd_config.d/99-swarm.conf

# Re-enable normal patching (docker-ce is protected by the hold above).
systemctl unmask unattended-upgrades.service apt-daily.service apt-daily.timer apt-daily-upgrade.service apt-daily-upgrade.timer 2>/dev/null || true
systemctl enable unattended-upgrades.service apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true

apt-get clean
echo "PROVISION_OK"
PROVISION

# --- 3. clean identity so clones don't collide ------------------------------
log "Cleaning identity (machine-id, ssh host keys, cloud-init state, logs)..."
ssh_node "$BUILDER_IP" "bash -s" <<'CLEAN'
set -uo pipefail
cloud-init clean --logs --seed 2>/dev/null || true
truncate -s 0 /etc/machine-id 2>/dev/null || true
rm -f /var/lib/dbus/machine-id 2>/dev/null || true
rm -f /etc/ssh/ssh_host_* 2>/dev/null || true
rm -rf /var/lib/cloud/instances/* /var/lib/cloud/instance 2>/dev/null || true
rm -f /root/.bash_history 2>/dev/null || true
find /var/log -type f -exec truncate -s 0 {} \; 2>/dev/null || true
rm -f /root/.ssh/authorized_keys 2>/dev/null || true   # DO re-injects the create-time key on clone
sync
echo "CLEAN_OK"
CLEAN

# --- 4. power off, snapshot, report -----------------------------------------
log "Powering off builder for a clean snapshot..."
doctl compute droplet-action power-off "$BUILDER_ID" --wait

log "Creating snapshot ${SNAPSHOT_NAME} (this can take several minutes)..."
doctl compute droplet-action snapshot "$BUILDER_ID" --snapshot-name "$SNAPSHOT_NAME" --wait

SNAP_ID="$(doctl compute snapshot list --resource droplet --format ID,Name --no-header | awk -v n="$SNAPSHOT_NAME" '$2==n {print $1}' | head -1)"
log "DONE."
echo
echo "==================================================================="
echo " Golden snapshot created:"
echo "   name: ${SNAPSHOT_NAME}"
echo "   id:   ${SNAP_ID:-<look it up: doctl compute snapshot list>}"
echo "   docker baked: ${DOCKER_VERSION} (held)"
echo
echo " Next: set DO_IMAGE to this snapshot id in the prod config, deploy the"
echo " spawner, and test ONE node off the snapshot before switching the fleet."
echo " See docs/golden-node-snapshot.md."
echo "==================================================================="
# builder droplet is destroyed by the EXIT trap.
