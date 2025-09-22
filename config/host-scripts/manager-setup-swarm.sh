#!/usr/bin/env bash
set -euo pipefail

# This script configures UFW on the manager and pins the Swarm data-path address


# to the manager's VPC IP (10.124.0.0/20). Run as root on the manager.

VPC_CIDR="10.124.0.0/20"

# Detect interfaces
VPC_IFACE=$(ip -o -4 addr show | awk '$4 ~ /10\.124\./ {print $2; exit 0}')
if [[ -z "${VPC_IFACE:-}" ]]; then
  echo "ERROR: Could not find an interface in ${VPC_CIDR}" >&2
  exit 1
fi
MANAGER_VPC_IP=$(ip -o -4 addr show dev "${VPC_IFACE}" | awk '{print $4}' | cut -d/ -f1 | head -n1)

# Try to guess a "public" iface (best-effort; may equal VPC_IFACE if single-NIC)
PUBLIC_IFACE=$(ip -o link show | awk -F': ' '$2 ~ /^e(th|np|ns)/ {print $2}' | head -n1)

echo "Detected VPC iface: ${VPC_IFACE} (${MANAGER_VPC_IP})"
echo "Detected public iface: ${PUBLIC_IFACE:-<none>}"

# -- Labels --

echo "Ensuring manager node has label 'ingress'"
MANAGER_ID=$(docker node ls --filter "role=manager" --format "{{.ID}}" | head -n1)
if [[ -n "${MANAGER_ID}" ]]; then
  docker node update --label-add ingress=true "${MANAGER_ID}"
else
  echo "Warning: Could not determine manager node ID to label" >&2
fi

# --- Networks ---
# Ensure required overlay networks exist (idempotent)
echo "Ensuring required overlay networks exist (caddy, jtlctl)"
for net in caddy jtlctl; do
  if docker network inspect "$net" >/dev/null 2>&1; then
    echo "Network '$net' already exists"
  else
    echo "Creating overlay network '$net'"
    docker network create --driver overlay --attachable "$net" || echo "Warning: failed to create network '$net'"
  fi
done


# --- Swarm pinning (manager) ---
# If no swarm is active, initialize with proper advertise/data-path addr.
# If a swarm is active, try 'swarm update'; if flag unsupported, advise safe re-init steps.
if docker info 2>/dev/null | grep -qi 'Swarm: active'; then
  echo "Swarm is active on this node."
else
  echo "Swarm is not active; initializing swarm with VPC IP ${MANAGER_VPC_IP}."
  docker swarm init --advertise-addr "${MANAGER_VPC_IP}" || true
fi



# Show overlay peers for a quick sanity-check (example for 'caddy' network)
if command -v jq >/dev/null 2>&1; then
  docker network inspect caddy >/dev/null 2>&1 && docker network inspect caddy | jq '.[0].Peers' || true
fi

echo "Done. Ensure workers join with --advertise-addr set to their 10.124.x IP."
